# encoding: utf-8


import uuid
from collections import MutableMapping

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction, IntegrityError

from apps import badgrlog
import requests_cache
from requests_cache.backends import BaseCache

import logging
from issuer.models import Issuer, BadgeClass, BadgeInstance
from issuer.utils import OBI_VERSION_CONTEXT_IRIS, convert_did_web_to_url
from mainsite.utils import first_node_match
import json
import hashlib


logger = logging.getLogger(__name__)

class DjangoCacheDict(MutableMapping):
    """TODO: Fix this class, its broken!"""
    _keymap_cache_key = "DjangoCacheDict_keys"

    def __init__(self, namespace, id=None, timeout=None):
        self.namespace = namespace
        self._timeout = timeout

        if id is None:
            id = uuid.uuid4().hexdigest()
        self._id = id
        self.keymap_cache_key = self._keymap_cache_key+"_"+self._id

    def build_key(self, *args):
        return "{keymap_cache_key}{namespace}{key}".format(
            keymap_cache_key=self.keymap_cache_key,
            namespace=self.namespace,
            key="".join(args)
        ).encode("utf-8")

    def timeout(self):
        return self._timeout

    def _keymap(self):
        keymap = cache.get(self.keymap_cache_key)
        if keymap is None:
            return []
        return keymap

    def __getitem__(self, key):
        result = cache.get(self.build_key(key))
        if result is None:
            raise KeyError
        return result

    def __setitem__(self, key, value):
        built_key = self.build_key(key)
        cache.set(built_key, value, timeout=self.timeout())

        # this probably needs locking...
        keymap = self._keymap()
        keymap.append(built_key)
        cache.set(self.keymap_cache_key, keymap, timeout=None)

    def __delitem__(self, key):
        built_key = self.build_key(key)
        cache.delete(built_key)

        # this probably needs locking...
        keymap = self._keymap()
        keymap.remove(built_key)
        cache.set(self.keymap_cache_key, keymap, timeout=None)

    def __len__(self):
        keymap = self._keymap()
        return len(keymap)

    def __iter__(self):
        keymap = self._keymap()
        for key in keymap:
            yield cache.get(key)

    def __str__(self):
        return '<{}>'.format(self.keymap_cache_key)

    def clear(self):
        self._id = uuid.uuid4().hexdigest()
        self.keymap_cache_key = self._keymap_cache_key+"_"+self._id


class OpenBadgesContextCache(BaseCache):
    OPEN_BADGES_CONTEXT_V2_URI = OBI_VERSION_CONTEXT_IRIS.get('2_0')
    OPEN_BADGE_CONTEXT_CACHE_KEY = 'OPEN_BADGE_CONTEXT_CACHE_KEY'
    FORTY_EIGHT_HOURS_IN_SECONDS = 60 * 60 * 24 * 2

    def __init__(self, *args, **kwargs):
        super(OpenBadgesContextCache, self).__init__(*args, **kwargs)

        cached_context = self._get_cached_content()

        if cached_context:
            self._intialize_instance_attributes(cached_context)
        else:
            self._set_cached_content()
            self._intialize_instance_attributes(self._get_cached_content())

    def _get_cached_content(self):
        return cache.get(self.OPEN_BADGE_CONTEXT_CACHE_KEY, None)

    def _set_cached_content(self):
        self.session = requests_cache.CachedSession(backend='memory', expire_after=300)
        response = self.session.get(self.OPEN_BADGES_CONTEXT_V2_URI, headers={'Accept': 'application/ld+json, application/json'})
        if response.status_code == 200:
            cache.set(
                self.OPEN_BADGE_CONTEXT_CACHE_KEY,
                {
                    'keys_map': self.session.cache.keys_map.copy(),
                    'response': self.session.cache.responses.copy()
                },
                timeout=self.FORTY_EIGHT_HOURS_IN_SECONDS
            )

    def _intialize_instance_attributes(self, cached):
        self.keys_map = cached.get('keys_map', None)
        self.responses = cached.get('response', None)


class DjangoCacheRequestsCacheBackend(BaseCache):
    def __init__(self, namespace='requests-cache', **options):
        super(DjangoCacheRequestsCacheBackend, self).__init__(**options)
        self.responses = DjangoCacheDict(namespace, 'responses')
        self.keys_map = DjangoCacheDict(namespace, 'urls')


class BadgeCheckHelper(object):
    _cache_instance = None
    error_map = [
        (['FETCH_HTTP_NODE'], {
            'name': "FETCH_HTTP_NODE",
            'description': "Unable to reach URL",
        }),
        (['VERIFY_RECIPIENT_IDENTIFIER'], {
            'name': 'VERIFY_RECIPIENT_IDENTIFIER',
            'description': "The recipient does not match any of your verified emails",
        }),
        (['VERIFY_JWS', 'VERIFY_KEY_OWNERSHIP'], {
            'name': "VERIFY_SIGNATURE",
            "description": "Could not verify signature",
        }),
        (['VERIFY_SIGNED_ASSERTION_NOT_REVOKED'], {
            'name': "ASSERTION_REVOKED",
            "description": "This assertion has been revoked",
        }),
    ]

    @classmethod
    def translate_errors(cls, badgecheck_messages):
        for m in badgecheck_messages:
            if m.get('messageLevel') == 'ERROR':
                for errors, backpack_error in cls.error_map:
                    if m.get('name') in errors:
                        yield backpack_error
                yield m

    @classmethod
    def cache_instance(cls):
        if cls._cache_instance is None:
            # TODO: note this class is broken and does not work correctly!
            cls._cache_instance = DjangoCacheRequestsCacheBackend(namespace='badgr_requests_cache')
        return cls._cache_instance

    @classmethod
    def badgecheck_options(cls):
        return getattr(settings, 'BADGECHECK_OPTIONS', {
            'include_original_json': True,
            'use_cache': True,
            # 'cache_backend': cls.cache_instance()  #  just use locmem cache for now
        })

    @classmethod
    def get_or_create_assertion(cls, url=None, imagefile=None, assertion=None, created_by=None):

        # distill 3 optional arguments into one query argument
        query = (url, imagefile, assertion)
        query = [v for v in query if v is not None]
        if len(query) != 1:
            raise ValueError("Must provide only 1 of: url, imagefile or assertion_obo")
        query = query[0]

        if created_by:
            emails = [d.email for d in created_by.email_items.all()]
            badgecheck_recipient_profile = {
                'email': emails + [v.email for v in created_by.cached_email_variants()],
                'telephone': created_by.cached_verified_phone_numbers(),
                'url': created_by.cached_verified_urls()
            }
        else:
            badgecheck_recipient_profile = None

        if isinstance(query, dict):
            badge_json = query
        else:
            try:
                badge_json = json.loads(query)
            except (TypeError, ValueError):
                raise ValidationError([{'name': "UNABLE_TO_VERIFY", 'description': "Unable to verify the assertion"}])            

        credentialSubject_json = badge_json.get("credentialSubject")
        if not credentialSubject_json:
            raise ValidationError([{'name': "ASSERTION_NOT_FOUND", 'description': "Unable to find credentialSubject in assertion"}])

        achievement_json = credentialSubject_json.get("achievement")
        if not achievement_json:
            raise ValidationError([{'name': "ASSERTION_NOT_FOUND", 'description': "Unable to find achievement in assertion's credentialSubject"}])
        
        if type(achievement_json) == str:
            try:
                achievement_response = requests_cache.CachedSession().get(achievement_json, headers={'Accept': 'application/ld+json, application/json'})
                if achievement_response.status_code == 200:
                    achievement_json = achievement_response.json()
                else:
                    raise ValidationError([{'name': "ASSERTION_NOT_FOUND", 'description': "Unable to find achievement: {}".format(achievement_response)}])
            except Exception as e:
                raise ValidationError([{'name': "ASSERTION_NOT_FOUND", 'description': f"Unable to find an achievement at {achievement_json}: {str(e)}"}])

        issuer_json = badge_json.get("issuer")
        if not issuer_json:
            raise ValidationError([{'name': "ASSERTION_NOT_FOUND", 'description': "Unable to find an issuer"}])
        
        if type(issuer_json) == str:
            try:
                url = convert_did_web_to_url(issuer_json, use_https=False)
                issuer_response = requests_cache.CachedSession().get(url, headers={'Accept': 'application/ld+json, application/json'})
                if issuer_response.status_code == 200:
                    issuer_json = issuer_response.json()
                else:
                    raise ValidationError([{'name': "ASSERTION_NOT_FOUND", 'description': "Unable to find an issuer: {}".format(issuer_response)}])
            except Exception as e:
                raise ValidationError([{'name': "ASSERTION_NOT_FOUND", 'description': f"Unable to find an issuer at {issuer_json}: {str(e)}"}])

        recipient_identifier = None
        recipient_type = None
        
        if badgecheck_recipient_profile:
            recipient_profile = credentialSubject_json.get('identifier', {})
            recipient_identifier = recipient_profile.get('identityHash', None)
            recipient_hashed = recipient_profile.get('hashed', False)
            recipient_salt = recipient_profile.get('salt', None)
            recipient_type = recipient_profile.get('identityType', 'email')
            if recipient_hashed:
                if not recipient_salt:
                    raise ValidationError([{'name': "MISSING_SALT", 'description': "Recipient identifier is hashed but no salt provided"}])
                
                candidates = (
                    badgecheck_recipient_profile.get('email', []) +
                    badgecheck_recipient_profile.get('telephone', []) +
                    badgecheck_recipient_profile.get('url', [])
                )

                match_found = False

                prefix = recipient_identifier.split('$')[0]
                if not prefix:
                    raise ValidationError([{'name': "INVALID_HASH_PREFIX", 'description': "Recipient identifier hash is missing a prefix"}])
                
                hashed_value = recipient_identifier.split('$')[1]
                if not hashed_value:
                    raise ValidationError([{'name': "INVALID_HASH_VALUE", 'description': "Recipient identifier hash is invalid"}])

                if prefix == 'sha256':
                    hashing = hashlib.sha256
                elif prefix == 'md5':
                    hashing = hashlib.md5
                else:
                    raise ValidationError([{'name': "UNSUPPORTED_HASH_PREFIX", 'description': "Recipient identifier hash has unsupported prefix: {}".format(prefix)}])

                for candidate in candidates:
                    digest = hashing((candidate + recipient_salt).encode()).hexdigest()
                    if digest == hashed_value:
                        match_found = True
                        break

                    digest = hashing((candidate.lower() + recipient_salt).encode()).hexdigest()
                    if digest == hashed_value:
                        match_found = True
                        break

                if not match_found:
                    raise ValidationError([{
                        'name': "RECIPIENT_MISMATCH",
                        'description': "Recipient does not match"
                    }])

            else:
                if recipient_identifier not in badgecheck_recipient_profile.get('email', []) + \
                                            badgecheck_recipient_profile.get('telephone', []) + \
                                            badgecheck_recipient_profile.get('url', []):
                    raise ValidationError([{'name': "RECIPIENT_MISMATCH", 'description': "Recipient does not match"}])

        #TODO: fix this since it's still not fully working, keep in mind this is to create badges that are imported
        issuer_image = Issuer.objects.image_from_ob3(issuer_json)
        badgeclass_image = BadgeClass.objects.image_from_ob3(achievement_json)
        badgeinstance_image = BadgeInstance.objects.image_from_ob3(badgeclass_image, badge_json)
       
        def commit_new_badge():
            with transaction.atomic():
                issuer = Issuer.objects.get_or_create_from_ob3(issuer_json, image=issuer_image, original_json=issuer_json)
                badgeclass = BadgeClass.objects.get_or_create_from_ob3(issuer, achievement_json, image=badgeclass_image, original_json=achievement_json)
                return BadgeInstance.objects.get_or_create_from_ob3(
                    badgeclass[0], issuer[0], badge_json,
                    recipient_identifier=recipient_identifier, recipient_type=recipient_type,
                    image=badgeinstance_image, original_json=badge_json
                )
        try:
            return commit_new_badge()
        except IntegrityError:
            logger.error("Race condition caught when saving new assertion: {}".format(query))
            return commit_new_badge()

    @classmethod
    def get_assertion_obo(cls, badge_instance):
        # try:
        #     response = openbadges.verify(badge_instance.source_url, recipient_profile=None, **cls.badgecheck_options())
        # except ValueError as e:
        #     return None

        # report = response.get('report', {})
        # is_valid = report.get('valid')

        # if is_valid:
        #     graph = response.get('graph', [])

        #     assertion_obo = first_node_match(graph, dict(type="Assertion"))
        #     if assertion_obo:
        #         return assertion_obo
        # TODO: Replace openbadges verification with direct retrieval of original_json from badge instance since badgecheck is currently broken and needs to be replaced with openbadges after next release
        return None
