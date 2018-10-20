import os
import logging
import requests
from uuid import uuid4
from pprint import pprint  # noqa
from banal import is_mapping, ensure_dict, ensure_list, hash_data
from urllib.parse import urljoin
from urlnormalizer import normalize_url
from followthemoney.exc import InvalidData

from corpint.common import Enricher, Result

log = logging.getLogger(__name__)


class AlephEnricher(Enricher):
    key_prefix = 'aleph'
    TYPE_CONSTRAINT = 'LegalEntity'

    def __init__(self):
        self.host = os.environ.get('ENRICH_ALEPH_HOST',
                                   'https://data.occrp.org/')
        self.api_base = urljoin(self.host, '/api/2/')
        self.api_key = os.environ.get('ENRICH_ALEPH_API_KEY')
        self.session = requests.Session()
        self.session.headers['X-Aleph-Session'] = str(uuid4())
        if self.api_key is not None:
            self.session.headers['Authorization'] = 'ApiKey %s' % self.api_key

    def get_api(self, url, params=None):
        if is_mapping(params):
            params = params.items()
        if params is not None:
            url = normalize_url(url, extra_query_args=params)

        data = self.cache.get(url)
        if data is None:
            res = self.session.get(url)
            if res.status_code != 200:
                return {}
            data = res.json()
            self.cache.store(url, data)
        return data

    def post_match(self, url, proxy, params=None):
        data = proxy.to_dict()
        key = hash_data((url, data))
        existing = self.cache.get(key)
        if existing is None:
            log.debug("Enrich [%s]: %s", self.host, proxy)
            res = self.session.post(url, json=data)
            if res.status_code != 200:
                return {}
            existing = res.json()
            self.cache.store(key, existing)
        return existing

    def convert_entity(self, result, data):
        data = ensure_dict(data)
        try:
            entity = result.make_entity(data.get('schema'))
        except InvalidData:
            log.error("Server model mismatch: %s" % data.get('schema'))
            return

        links = ensure_dict(data.get('link'))
        entity.make_id(links.get('self'))
        entity.add('alephUrl', links.get('self'))
        properties = ensure_dict(data.get('properties'))
        for prop, values in properties.items():
            for value in ensure_list(values):
                if is_mapping(value):
                    child = self.convert_entity(result, value)
                    if child.id is None:
                        continue
                    value = child.id
                try:
                    entity.add(prop, value, cleaned=True)
                except InvalidData:
                    msg = "Server property mismatch (%s): %s"
                    log.warning(msg % (entity.schema.name, prop))
        return entity

    def enrich_entity(self, entity):
        if not entity.schema.matchable:
            return

        url = urljoin(self.api_base, 'match')
        for page in range(10):
            data = self.post_match(url, entity)
            for res in data.get('results', []):
                result = Result(self)
                proxy = self.convert_entity(result, res)
                if proxy is None:
                    continue
                result.principal = proxy
                yield result

            url = data.get('next')
            if url is None:
                break