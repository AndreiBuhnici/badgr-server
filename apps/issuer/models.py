import io
import datetime
import urllib.request, urllib.parse, urllib.error
import requests

import base58
import dateutil
import re
import uuid
from collections import OrderedDict
from itertools import chain

from nacl import signing
import hashlib

import cachemodel
import os
from allauth.account.adapter import get_adapter
from cachemodel import CACHE_FOREVER_TIMEOUT
from cachemodel.utils import generate_cache_key
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.urls import reverse
from django.db import models, transaction
from django.db.models import ProtectedError
from json import loads as json_loads
from json import dumps as json_dumps
from pyld import jsonld

from jsonfield import JSONField
from openbadges_bakery import bake
from django.utils import timezone
from django.core.cache import cache

import badgrlog
from entity.models import BaseVersionedEntity
from issuer.managers import BadgeInstanceManager, IssuerManager, BadgeClassManager, BadgeInstanceEvidenceManager
from mainsite.managers import SlugOrJsonIdCacheModelManager
from mainsite.mixins import HashUploadedImage, ResizeUploadedImage, ScrubUploadedSvgImage, PngImagePreview
from mainsite.models import BadgrApp, EmailBlacklist
from mainsite import blacklist
from mainsite.utils import OriginSetting, generate_entity_uri

from .utils import (add_obi_version_ifneeded, CURRENT_OBI_VERSION, convert_did_web_to_url, convert_url_to_did_web, convert_url_to_did_web, decrypt_value, encrypt_value, generate_rebaked_filename,
                    generate_sha256_hashstring, get_credentials_context, get_did_context, get_obi_context, parse_original_datetime, UNVERSIONED_BAKED_VERSION)

AUTH_USER_MODEL = getattr(settings, 'AUTH_USER_MODEL', 'auth.User')

RECIPIENT_TYPE_EMAIL = 'email'
RECIPIENT_TYPE_ID = 'openBadgeId'
RECIPIENT_TYPE_TELEPHONE = 'telephone'
RECIPIENT_TYPE_URL = 'url'

logger = badgrlog.BadgrLogger()


class BaseAuditedModel(cachemodel.CacheModel):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey('badgeuser.BadgeUser', blank=True, null=True, related_name="+",
                                   on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    updated_by = models.ForeignKey('badgeuser.BadgeUser', blank=True, null=True, related_name="+",
                                   on_delete=models.SET_NULL)

    class Meta:
        abstract = True

    @property
    def cached_creator(self):
        from badgeuser.models import BadgeUser
        return BadgeUser.cached.get(id=self.created_by_id)


class BaseAuditedModelDeletedWithUser(cachemodel.CacheModel):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    created_by = models.ForeignKey('badgeuser.BadgeUser', blank=True, null=True, related_name="+",
                                   on_delete=models.CASCADE)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)
    updated_by = models.ForeignKey('badgeuser.BadgeUser', blank=True, null=True, related_name="+",
                                   on_delete=models.CASCADE)

    class Meta:
        abstract = True

    @property
    def cached_creator(self):
        from badgeuser.models import BadgeUser
        return BadgeUser.cached.get(id=self.created_by_id)


class OriginalJsonMixin(models.Model):
    original_json = models.TextField(blank=True, null=True, default=None)

    class Meta:
        abstract = True

    def get_original_json(self):
        if self.original_json:
            try:
                return json_loads(self.original_json)
            except (TypeError, ValueError) as e:
                pass

    def get_filtered_json(self, excluded_fields=()):
        original = self.get_original_json()
        if original is not None:
            return {key: original[key] for key in [k for k in list(original.keys()) if k not in excluded_fields]}


class BaseOpenBadgeObjectModel(OriginalJsonMixin, cachemodel.CacheModel):
    source = models.CharField(max_length=254, default='local')
    source_url = models.CharField(max_length=254, blank=True, null=True, default=None, unique=True)

    class Meta:
        abstract = True

    def __hash__(self):
        return hash((self.source, self.source_url))

    def __eq__(self, other):
        UNUSABLE_DEFAULT = uuid.uuid4()

        comparable_properties = getattr(self, 'COMPARABLE_PROPERTIES', None)
        if comparable_properties is None:
            return super(BaseOpenBadgeObjectModel, self).__eq__(other)

        for prop in self.COMPARABLE_PROPERTIES:
            if getattr(self, prop) != getattr(other, prop, UNUSABLE_DEFAULT):
                return False
        return True

class BaseOpenBadgeExtension(cachemodel.CacheModel):
    name = models.CharField(max_length=254)
    original_json = models.TextField(blank=True, null=True, default=None)

    def __str__(self):
        return self.name

    class Meta:
        abstract = True


class Issuer(ResizeUploadedImage,
             ScrubUploadedSvgImage,
             PngImagePreview,
             BaseAuditedModel,
             BaseVersionedEntity,
             BaseOpenBadgeObjectModel,
             cachemodel.CacheModel):
    entity_class_name = 'Profile'
    COMPARABLE_PROPERTIES = ('badgrapp_id', 'description', 'email', 'entity_id', 'entity_version', 'name', 'pk',
                            'updated_at', 'url',)

    staff = models.ManyToManyField(AUTH_USER_MODEL, through='IssuerStaff')

    # slug has been deprecated for now, but preserve existing values
    slug = models.CharField(max_length=255, db_index=True, blank=True, null=True, default=None)
    #slug = AutoSlugField(max_length=255, populate_from='name', unique=True, blank=False, editable=True)

    badgrapp = models.ForeignKey('mainsite.BadgrApp', blank=True, null=True, default=None, on_delete=models.SET_NULL)

    name = models.CharField(max_length=1024)
    image = models.FileField(upload_to='uploads/issuers', blank=True, null=True)
    image_preview = models.FileField(upload_to='uploads/issuers', blank=True, null=True)
    description = models.TextField(blank=True, null=True, default=None)
    url = models.CharField(max_length=254, blank=True, null=True, default=None)
    email = models.CharField(max_length=254, blank=True, null=True, default=None)
    old_json = JSONField()
    main_signing_key = models.ForeignKey('IssuerKey', null=True, blank=True, on_delete=models.SET_NULL, related_name='+')

    objects = IssuerManager()
    cached = SlugOrJsonIdCacheModelManager(slug_kwarg_name='entity_id', slug_field_name='entity_id')

    @classmethod
    def get_all_issuers(cls):
        return cls.cached.all()

    def publish(self, publish_staff=True, *args, **kwargs):
        fields_cache = self._state.fields_cache  # stash the fields cache to avoid publishing related objects here
        self._state.fields_cache = dict()

        super(Issuer, self).publish(*args, **kwargs)
        if publish_staff:
            for member in self.cached_issuerstaff():
                member.cached_user.publish()

        self._state.fields_cache = fields_cache  # restore the fields cache

    def has_nonrevoked_assertions(self):
        return self.badgeinstance_set.filter(revoked=False).exists()

    def delete(self, *args, **kwargs):
        if self.has_nonrevoked_assertions():
            raise ProtectedError("Issuer can not be deleted because it has previously issued badges.", self)

        # remove any unused badgeclasses owned by issuer
        for bc in self.cached_badgeclasses():
            bc.delete()

        staff = self.cached_issuerstaff()
        ret = super(Issuer, self).delete(*args, **kwargs)

        # remove membership records
        for membership in staff:
            membership.delete(publish_issuer=False)

        if apps.is_installed('badgebook'):
            # badgebook shim
            try:
                from badgebook.models import LmsCourseInfo
                # update LmsCourseInfo's that were using this issuer as the default_issuer
                for course_info in LmsCourseInfo.objects.filter(default_issuer=self):
                    course_info.default_issuer = None
                    course_info.save()
            except ImportError:
                pass

        return ret

    def _badge_user_cache_key_cached_issuers(self, user_id):
        return generate_cache_key(
            ["BadgeUser", "cached_issuers_current_user", user_id]
        )

    def upload_to_issuer_registry(self):
        fabric_gateway_url = getattr(settings, 'FABRIC_GATEWAY_URL')
        chaincode = getattr(settings, 'ISSUER_CHAINCODE')
        methods = [
            {
                "id": f'{self.did_id}#{k.key_fragment}',
                "validFrom": k.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "validUntil": k.validUntil.strftime("%Y-%m-%dT%H:%M:%SZ") if k.validUntil else ''
            } 
            for k in self.keys.all()
        ]
        
        response = requests.post(
            url = f"{fabric_gateway_url}/submit",
            headers = {"Content-Type": "application/json"},
            json = {"chaincode": chaincode, "transaction": "RegisterIssuer", "args": [
                self.did_id,
                self.name,
                json_dumps(methods)
            ]}
        )
        
        if response.status_code == 200:
            resp_json = response.json()
            logger.logger.info(f"Json loaded {resp_json}")
            if resp_json.get("ok"):
                logger.logger.info(f"Issuer {self.did_id} successfully registered on issuer registry.")
            else:
                raise ValidationError(f"Fabric rejected submit: {response.text}")
        else:
            logger.logger.error(f"HTTP error: {response.status_code} {response.text}")
            raise ValidationError("Could not register issuer on issuer registry.")

    def save(self, *args, **kwargs):
        if self.pk is None:
            try:
                with transaction.atomic():
                    super(Issuer, self).save(*args, **kwargs)

                    # if no owner staff records exist, create one for created_by
                    if len(self.owners) < 1 and self.created_by_id:
                        IssuerStaff.objects.create(issuer=self, user=self.created_by, role=IssuerStaff.ROLE_OWNER)
                    
                    self._generate_initial_key()

                    #self.upload_to_issuer_registry()
            except Exception as e:
                # In case transaction fails delete cached issuers of current user
                cache.delete(self._badge_user_cache_key_cached_issuers(self.created_by_id))
                raise e
        else:
            super(Issuer, self).save(*args, **kwargs)
    
    def _generate_initial_key(self):
        signing_service_url = getattr(settings, 'SIGNING_SERVICE_URL')
        payload = {
            "issuerDid": self.did_id,
            "purpose": 'assertionMethod'
        }
        response = requests.post(f'{signing_service_url}/keys', json=payload, timeout=10)

        if response.status_code != 200:
            raise ValidationError(f"Proof service error: {response.text}")

        data = response.json()
        if not data.get("ok"):
            raise ValidationError(data.get("error"))

        key_fragment = data.get("key", {}).get("keyFragment")
        if not key_fragment:
            raise ValidationError("Missing keyFragment")

        issuer_key = IssuerKey.objects.create(
            issuer=self,
            key_fragment=key_fragment,
            purpose="assertionMethod",
            is_active=True
        )

        Issuer.objects.filter(pk=self.pk).update(main_signing_key=issuer_key)

    def get_absolute_url(self):
        return reverse('issuer_json', kwargs={'entity_id': self.entity_id})

    @property
    def public_url(self):
        return OriginSetting.HTTP+self.get_absolute_url()

    def image_url(self, public=False):
        if bool(self.image):
            if public:
                return OriginSetting.HTTP + reverse('issuer_image', kwargs={'entity_id': self.entity_id})
            if getattr(settings, 'MEDIA_URL').startswith('http'):
                return default_storage.url(self.image.name)
            else:
                return getattr(settings, 'HTTP_ORIGIN') + default_storage.url(self.image.name)
        else:
            return None

    @property
    def jsonld_id(self):
        if self.source_url:
            return self.source_url
        return OriginSetting.HTTP + self.get_absolute_url()

    @property
    def did_id(self):
        return convert_url_to_did_web(self.jsonld_id)

    @property
    def editors(self):
        return self.staff.filter(issuerstaff__role__in=(IssuerStaff.ROLE_EDITOR, IssuerStaff.ROLE_OWNER))

    @property
    def owners(self):
        return self.staff.filter(issuerstaff__role=IssuerStaff.ROLE_OWNER)

    @cachemodel.cached_method(auto_publish=True)
    def cached_issuerstaff(self):
        return IssuerStaff.objects.filter(issuer=self)

    @property
    def staff_items(self):
        return self.cached_issuerstaff()

    @property
    def main_verification_method(self):
        if self.main_signing_key:
            return f'{self.did_id}#{self.main_signing_key.key_fragment}'
        return None

    @staff_items.setter
    def staff_items(self, value):
        """
        Update this issuers IssuerStaff from a list of IssuerStaffSerializerV2 data
        """
        existing_staff_idx = {s.cached_user: s for s in self.staff_items}
        new_staff_idx = {s['cached_user']: s for s in value}

        with transaction.atomic():
            # add missing staff records
            for staff_data in value:
                if staff_data['cached_user'] not in existing_staff_idx:
                    staff_record, created = IssuerStaff.cached.get_or_create(
                        issuer=self,
                        user=staff_data['cached_user'],
                        defaults={
                            'role': staff_data['role']
                        })
                    if not created:
                        staff_record.role = staff_data['role']
                        staff_record.save()

            # remove old staff records -- but never remove the only OWNER role
            for staff_record in self.staff_items:
                if staff_record.cached_user not in new_staff_idx:
                    if staff_record.role != IssuerStaff.ROLE_OWNER or len(self.owners) > 1:
                        staff_record.delete()

    def get_extensions_manager(self):
        return self.issuerextension_set

    @cachemodel.cached_method(auto_publish=True)
    def cached_editors(self):
        UserModel = get_user_model()
        return UserModel.objects.filter(issuerstaff__issuer=self, issuerstaff__role=IssuerStaff.ROLE_EDITOR)

    @cachemodel.cached_method(auto_publish=True)
    def cached_badgeclasses(self):
        return self.badgeclasses.all().order_by("created_at")

    @property
    def image_preview(self):
        return self.image

    def _get_public_key_multibase(self, key_fragment):
        signing_service_url = getattr(settings, 'SIGNING_SERVICE_URL')

        response = requests.get(f"{signing_service_url}/keys", params={
            "issuerDid": self.did_id,
            "keyFragment": key_fragment
        }, timeout=5)

        if response.status_code != 200:
            raise ValueError(f"Could not fetch key {key_fragment}: {response.text}")

        data = response.json()
        public_multibase = data.get('key', {}).get('public_key_multibase')
        if not public_multibase:
            raise ValueError(f"No public key returned for {key_fragment}")
        
        return public_multibase

    def get_json(self, obi_version=CURRENT_OBI_VERSION, include_extra=True):
        _, ob_context_iri = get_obi_context(obi_version)
        _, did_context_iri = get_did_context('1_0')
        _, credentials_context_iri = get_credentials_context('2_0')

        json = OrderedDict({'@context': [credentials_context_iri, did_context_iri, ob_context_iri]})

        json.update(OrderedDict(
            type='Profile',
            id=self.did_id,
            name=self.name,
            url=self.url,
            email=self.email,
            description=self.description))
        
        image_url = self.image_url(public=True)
        json['image'] = image_url
        if self.original_json:
            image_info = self.get_original_json().get('image', None)
            if isinstance(image_info, dict):
                json['image'] = image_info
                json['image']['id'] = image_url

        active_keys = self.keys.filter(is_active=True)
        verification_method = []
        json['authentication'] = []
        json['assertionMethod'] = []
        json['keyAgreement'] = []

        for key in active_keys:
            verification_method.append({
                "id": f"{self.did_id}#{key.key_fragment}",
                "type": "Multikey",
                "controller": self.did_id,
                "publicKeyMultibase": self._get_public_key_multibase(key.key_fragment),
            })

            if key.purpose in json:
                json[key.purpose].append(f"{self.did_id}#{key.key_fragment}")
        
        json['verificationMethod'] = verification_method

        # pass through imported json
        if include_extra:
            extra = self.get_filtered_json()
            if extra is not None:
                for k,v in list(extra.items()):
                    if k not in json:
                        json[k] = v

        return json

    @property
    def json(self):
        return self.get_json()

    def get_filtered_json(self, excluded_fields=('@context', 'id', 'type', 'name', 'url', 'description', 'image', 'email')):
        return super(Issuer, self).get_filtered_json(excluded_fields=excluded_fields)

    @property
    def cached_badgrapp(self):
        id = self.badgrapp_id if self.badgrapp_id else None
        return BadgrApp.objects.get_by_id_or_default(badgrapp_id=id)

    def has_nonrevoked_assertions(self):
        return self.badgeinstance_set.filter(revoked=False).exists()

class IssuerKey(models.Model):
    issuer = models.ForeignKey("Issuer", related_name="keys", on_delete=models.CASCADE)

    key_fragment = models.CharField(max_length=50)
    purpose = models.CharField(max_length=50, default="assertionMethod")

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("issuer", "key_fragment")

class IssuerStaff(cachemodel.CacheModel):
    ROLE_OWNER = 'owner'
    ROLE_EDITOR = 'editor'
    ROLE_STAFF = 'staff'
    ROLE_CHOICES = (
        (ROLE_OWNER, 'Owner'),
        (ROLE_EDITOR, 'Editor'),
        (ROLE_STAFF, 'Staff'),
    )
    issuer = models.ForeignKey(Issuer,
                               on_delete=models.CASCADE)
    user = models.ForeignKey(AUTH_USER_MODEL,
                             on_delete=models.CASCADE)
    role = models.CharField(max_length=254, choices=ROLE_CHOICES, default=ROLE_STAFF)

    class Meta:
        unique_together = ('issuer', 'user')

    def publish(self):
        super(IssuerStaff, self).publish()
        self.issuer.publish(publish_staff=False)
        self.user.publish()

    def delete(self, *args, **kwargs):
        publish_issuer = kwargs.pop('publish_issuer', True)
        super(IssuerStaff, self).delete()
        if publish_issuer:
            self.issuer.publish(publish_staff=False)
        self.user.publish()

    @property
    def cached_user(self):
        from badgeuser.models import BadgeUser
        return BadgeUser.cached.get(pk=self.user_id)

    @property
    def cached_issuer(self):
        return Issuer.cached.get(pk=self.issuer_id)


def get_user_or_none(recipient_id, recipient_type):
    from badgeuser.models import UserRecipientIdentifier, CachedEmailAddress
    user = None
    if recipient_type == 'email':
        verified_email = CachedEmailAddress.objects.filter(verified=True, email=recipient_id).first()
        if verified_email:
            user = verified_email.user
    else:
        verified_recipient_id = UserRecipientIdentifier.objects.filter(verified=True,
                                                                       identifier=recipient_id).first()
        if verified_recipient_id:
            user = verified_recipient_id.user

    return user


class BadgeClass(ResizeUploadedImage,
                 ScrubUploadedSvgImage,
                 HashUploadedImage,
                 PngImagePreview,
                 BaseAuditedModel,
                 BaseVersionedEntity,
                 BaseOpenBadgeObjectModel):
    entity_class_name = 'Achievement'
    COMPARABLE_PROPERTIES = ('criteria_text', 'criteria_url', 'description', 'entity_id', 'entity_version',
                             'expires_amount', 'expires_duration', 'name', 'pk', 'slug', 'updated_at',)

    EXPIRES_DURATION_DAYS = 'days'
    EXPIRES_DURATION_WEEKS = 'weeks'
    EXPIRES_DURATION_MONTHS = 'months'
    EXPIRES_DURATION_YEARS = 'years'
    EXPIRES_DURATION_CHOICES = (
        (EXPIRES_DURATION_DAYS, 'Days'),
        (EXPIRES_DURATION_WEEKS, 'Weeks'),
        (EXPIRES_DURATION_MONTHS, 'Months'),
        (EXPIRES_DURATION_YEARS, 'Years'),
    )

    issuer = models.ForeignKey(Issuer, blank=False, null=False, on_delete=models.CASCADE, related_name="badgeclasses")

    # slug has been deprecated for now, but preserve existing values
    slug = models.CharField(max_length=255, db_index=True, blank=True, null=True, default=None)
    #slug = AutoSlugField(max_length=255, populate_from='name', unique=True, blank=False, editable=True)

    name = models.CharField(max_length=255)
    image = models.FileField(upload_to='uploads/badges', blank=True)
    image_preview = models.FileField(upload_to='uploads/badges', blank=True, null=True)
    description = models.TextField(blank=True, null=True, default=None)

    criteria_url = models.CharField(max_length=254, blank=True, null=True, default=None)
    criteria_text = models.TextField(blank=True, null=True)

    expires_amount = models.IntegerField(blank=True, null=True, default=None)
    expires_duration = models.CharField(max_length=254, choices=EXPIRES_DURATION_CHOICES, blank=True, null=True, default=None)

    old_json = JSONField()

    objects = BadgeClassManager()
    cached = SlugOrJsonIdCacheModelManager(slug_kwarg_name='entity_id', slug_field_name='entity_id')

    class Meta:
        verbose_name_plural = "Badge classes"

    def publish(self):
        fields_cache = self._state.fields_cache  # stash the fields cache to avoid publishing related objects here
        self._state.fields_cache = dict()
        super(BadgeClass, self).publish()
        self.issuer.publish(publish_staff=False)
        if self.created_by:
            self.created_by.publish()

        self._state.fields_cache = fields_cache  # restore the fields cache

    def delete(self, *args, **kwargs):
        # if there are some assertions that have not expired
        if self.badgeinstances.filter(revoked=False).filter(
                models.Q(validUntil__isnull=True) | models.Q(validUntil__gt=timezone.now())).exists():
            raise ProtectedError("BadgeClass may only be deleted if all BadgeInstances have been revoked.", self)

        issuer = self.issuer
        super(BadgeClass, self).delete(*args, **kwargs)
        issuer.publish(publish_staff=False)

    def schedule_image_update_task(self):
        from issuer.tasks import rebake_all_assertions_for_badge_class
        batch_size = getattr(settings, 'BADGE_ASSERTION_AUTO_REBAKE_BATCH_SIZE', 100)
        rebake_all_assertions_for_badge_class.delay(self.pk, limit=batch_size, replay=True)

    def get_absolute_url(self):
        return reverse('badgeclass_json', kwargs={'entity_id': self.entity_id})


    @property
    def public_url(self):
        return OriginSetting.HTTP+self.get_absolute_url()

    @property
    def jsonld_id(self):
        if self.source_url:
            return self.source_url
        return OriginSetting.HTTP + self.get_absolute_url()

    @property
    def issuer_jsonld_id(self):
        return self.cached_issuer.jsonld_id

    def get_criteria_url(self):
        if self.criteria_url:
            return self.criteria_url
        return OriginSetting.HTTP+reverse('badgeclass_criteria', kwargs={'entity_id': self.entity_id})

    @property
    def description_nonnull(self):
        return self.description if self.description else ""

    @description_nonnull.setter
    def description_nonnull(self, value):
        self.description = value

    @property
    def owners(self):
        return self.cached_issuer.owners

    @property
    def cached_issuer(self):
        return Issuer.cached.get(pk=self.issuer_id)

    def has_nonrevoked_assertions(self):
        return self.badgeinstances.filter(revoked=False).exists()

    """
    Included for legacy purposes. It is inefficient to routinely call this for badge classes with large numbers of assertions.
    """
    @property
    def v1_api_recipient_count(self):
        return self.badgeinstances.filter(revoked=False).count()

    @cachemodel.cached_method(auto_publish=True)
    def cached_alignments(self):
        return self.badgeclassalignment_set.all()

    @property
    def alignment_items(self):
        return self.cached_alignments()

    @alignment_items.setter
    def alignment_items(self, value):
        if value is None:
            value = []
        keys = ['target_name','target_url','target_description','target_framework', 'target_code']

        def _identity(align):
            """build a unique identity from alignment json"""
            return "&".join("{}={}".format(k, align.get(k, None)) for k in keys)

        def _obj_identity(alignment):
            """build a unique identity from alignment json"""
            return "&".join("{}={}".format(k, getattr(alignment, k)) for k in keys)

        existing_idx = {_obj_identity(a): a for a in self.alignment_items}
        new_idx = {_identity(a): a for a in value}

        with transaction.atomic():
            # HACKY, but force a save to self otherwise we can't create related objects here
            if not self.pk:
                self.save()

            # add missing records
            for align in value:
                if _identity(align) not in existing_idx:
                    alignment = self.badgeclassalignment_set.create(**align)

            # remove old records
            for alignment in self.alignment_items:
                if _obj_identity(alignment) not in new_idx:
                    alignment.delete()

    @cachemodel.cached_method(auto_publish=True)
    def cached_tags(self):
        return self.badgeclasstag_set.all()

    @property
    def tag_items(self):
        return self.cached_tags()

    @tag_items.setter
    def tag_items(self, value):
        if value is None:
            value = []
        existing_idx = [t.name for t in self.tag_items]
        new_idx = value

        with transaction.atomic():
            if not self.pk:
                self.save()

            # add missing
            for t in value:
                if t not in existing_idx:
                    tag = self.badgeclasstag_set.create(name=t)

            # remove old
            for tag in self.tag_items:
                if tag.name not in new_idx:
                    tag.delete()

    def get_extensions_manager(self):
        return self.badgeclassextension_set

    def issue(self, recipient_id=None, evidence=None, narrative=None, notify=False, created_by=None, allow_uppercase=False, badgr_app=None, recipient_type=RECIPIENT_TYPE_EMAIL, **kwargs):
        return BadgeInstance.objects.create(
            badgeclass=self, recipient_identifier=recipient_id, recipient_type=recipient_type,
            narrative=narrative, evidence=evidence,
            notify=notify, created_by=created_by, allow_uppercase=allow_uppercase,
            badgr_app=badgr_app,
            user=get_user_or_none(recipient_id, recipient_type),
            **kwargs
        )

    def image_url(self, public=False):
        if public:
            return OriginSetting.HTTP + reverse('badgeclass_image', kwargs={'entity_id': self.entity_id})

        if getattr(settings, 'MEDIA_URL').startswith('http'):
            return default_storage.url(self.image.name)
        else:
            return getattr(settings, 'HTTP_ORIGIN') + default_storage.url(self.image.name)

    def get_json(self, obi_version=CURRENT_OBI_VERSION, include_extra=True):
        obi_version, context_iri = get_obi_context(obi_version)
        json = OrderedDict({'@context': context_iri})

        json.update(OrderedDict(
            type='Achievement',
            id=self.jsonld_id,
            name=self.name,
            description=self.description_nonnull,
            creator={}
        ))

        issuer_json = self.cached_issuer.json

        json['creator']['id'] = issuer_json['id']
        json['creator']['type'] = issuer_json['type']
        json['creator']['name'] = issuer_json['name']
        json['creator']['image'] = issuer_json['image']
        json['creator']['url'] = issuer_json['url']
        json['creator']['description'] = issuer_json['description']
        json['creator']['email'] = issuer_json['email']

        # image
        if self.image:
            image_url = self.image_url(public=True)
            json['image'] = image_url
            if self.original_json:
                original_json = self.get_original_json()
                if original_json is not None:
                    image_info = original_json.get('image', None)
                    if isinstance(image_info, dict):
                        json['image'] = image_info
                        json['image']['id'] = image_url

        # criteria
        if obi_version == '1_1':
            json["criteria"] = self.get_criteria_url()
        elif obi_version == '2_0' or obi_version == '3_0':
            json["criteria"] = {}
            if self.criteria_url:
                json['criteria']['id'] = self.criteria_url
            if self.criteria_text:
                json['criteria']['narrative'] = self.criteria_text

        # alignment / tag
        if obi_version == '2_0' or obi_version == '3_0':
            json['alignment'] = [ a.get_json(obi_version=obi_version) for a in self.cached_alignments() ]
            json['tag'] = list(t.name for t in self.cached_tags())

        # pass through imported json
        if include_extra:
            extra = self.get_filtered_json()
            if extra is not None:
                for k,v in list(extra.items()):
                    if k not in json:
                        json[k] = v

        return json

    @property
    def json(self):
        return self.get_json()

    def get_filtered_json(self, excluded_fields=('@context', 'id', 'type', 'name', 'description', 'image', 'criteria', 'issuer')):
        return super(BadgeClass, self).get_filtered_json(excluded_fields=excluded_fields)

    @property
    def cached_badgrapp(self):
        return self.cached_issuer.cached_badgrapp

    def generate_validUntil(self, validFrom=None):
        if not self.expires_duration or not self.expires_amount:
            return None

        if validFrom is None:
            validFrom = timezone.now()

        duration_kwargs = dict()
        duration_kwargs[self.expires_duration] = self.expires_amount
        return validFrom + dateutil.relativedelta.relativedelta(**duration_kwargs)


class BadgeInstance(BaseAuditedModel,
                    BaseVersionedEntity,
                    BaseOpenBadgeObjectModel):
    entity_class_name = 'VerifiableCredential'
    COMPARABLE_PROPERTIES = ('badgeclass_id', 'entity_id', 'entity_version', 'validFrom', 'pk', 'narrative',
                             'recipient_identifier', 'recipient_type', 'revoked', 'revocation_reason', 'updated_at',)

    validFrom = models.DateTimeField(blank=False, null=False, default=timezone.now)

    badgeclass = models.ForeignKey(BadgeClass, blank=False, null=False, on_delete=models.CASCADE, related_name='badgeinstances')
    issuer = models.ForeignKey(Issuer, blank=False, null=False,
                               on_delete=models.CASCADE)
    user = models.ForeignKey('badgeuser.BadgeUser', blank=True, null=True, on_delete=models.SET_NULL)

    RECIPIENT_TYPE_CHOICES = (
        (RECIPIENT_TYPE_EMAIL, 'email'),
        (RECIPIENT_TYPE_ID, 'openBadgeId'),
        (RECIPIENT_TYPE_TELEPHONE, 'telephone'),
        (RECIPIENT_TYPE_URL, 'url'),
    )
    recipient_identifier = models.CharField(max_length=384, blank=False, null=False, db_index=True)
    recipient_type = models.CharField(max_length=255, choices=RECIPIENT_TYPE_CHOICES, default=RECIPIENT_TYPE_EMAIL, blank=False, null=False)

    image = models.FileField(upload_to='uploads/badges', blank=True)

    # slug has been deprecated for now, but preserve existing values
    slug = models.CharField(max_length=255, db_index=True, blank=True, null=True, default=None)
    #slug = AutoSlugField(max_length=255, populate_from='get_new_slug', unique=True, blank=False, editable=False)

    revoked = models.BooleanField(default=False, db_index=True)
    revocation_reason = models.CharField(max_length=255, blank=True, null=True, default=None)

    validUntil = models.DateTimeField(blank=True, null=True, default=None)

    ACCEPTANCE_UNACCEPTED = 'Unaccepted'
    ACCEPTANCE_ACCEPTED = 'Accepted'
    ACCEPTANCE_REJECTED = 'Rejected'
    ACCEPTANCE_CHOICES = (
        (ACCEPTANCE_UNACCEPTED, 'Unaccepted'),
        (ACCEPTANCE_ACCEPTED, 'Accepted'),
        (ACCEPTANCE_REJECTED, 'Rejected'),
    )
    acceptance = models.CharField(max_length=254, choices=ACCEPTANCE_CHOICES, default=ACCEPTANCE_UNACCEPTED)

    hashed = models.BooleanField(default=True)
    salt = models.CharField(max_length=254, blank=True, null=True, default=None)

    narrative = models.TextField(blank=True, null=True, default=None)

    old_json = JSONField()

    objects = BadgeInstanceManager()
    cached = SlugOrJsonIdCacheModelManager(slug_kwarg_name='entity_id', slug_field_name='entity_id')

    class Meta:
        index_together = (
                ('recipient_identifier', 'badgeclass', 'revoked'),
        )

    @property
    def extended_json(self):
        extended_json = self.json
        extended_json['badge'] = self.badgeclass.json
        extended_json['badge']['issuer'] = self.issuer.json

        return extended_json

    def image_url(self, public=False):
        if public:
            return OriginSetting.HTTP + reverse('badgeinstance_image', kwargs={'entity_id': self.entity_id})
        if getattr(settings, 'MEDIA_URL').startswith('http'):
            return default_storage.url(self.image.name)
        else:
            return getattr(settings, 'HTTP_ORIGIN') + default_storage.url(self.image.name)

    def get_share_url(self, include_identifier=False):
        url = self.share_url
        if include_identifier:
            url = '%s?identity__%s=%s' % (url, self.recipient_type, urllib.parse.quote(self.recipient_identifier))
        return url

    @property
    def share_url(self):
        return self.public_url
        # return OriginSetting.HTTP+reverse('backpack_shared_assertion', kwargs={'share_hash': self.entity_id})

    @property
    def cached_issuer(self):
        return Issuer.cached.get(pk=self.issuer_id)

    @property
    def cached_badgeclass(self):
        return BadgeClass.cached.get(pk=self.badgeclass_id)

    def get_absolute_url(self):
        return reverse('badgeinstance_json', kwargs={'entity_id': self.entity_id})

    @property
    def jsonld_id(self):
        if self.source_url:
            return self.source_url
        return OriginSetting.HTTP + self.get_absolute_url()

    @property
    def urn_id(self):
        return 'urn:uuid:' + self.entity_id

    @property
    def badgeclass_jsonld_id(self):
        return self.cached_badgeclass.jsonld_id

    @property
    def issuer_jsonld_id(self):
        return self.cached_issuer.jsonld_id

    @property
    def public_url(self):
        return OriginSetting.HTTP+self.get_absolute_url()

    @property
    def owners(self):
        return self.issuer.owners

    @property
    def pending(self):
        """
            If the associated identifier for this BadgeInstance
            does not exist or is unverified the BadgeInstance is
            considered "pending"
        """
        from badgeuser.models import CachedEmailAddress, UserRecipientIdentifier
        try:
            if self.recipient_type == RECIPIENT_TYPE_EMAIL:
                existing_identifier = CachedEmailAddress.cached.get(email=self.recipient_identifier)
            else:
                existing_identifier = UserRecipientIdentifier.cached.get(identifier=self.recipient_identifier)
        except (UserRecipientIdentifier.DoesNotExist, CachedEmailAddress.DoesNotExist,):
            return False

        if not self.source_url:
            return False

        return not existing_identifier.verified

    def _badge_user_cache_key_cached_badgeinstances(self, user_id):
        return generate_cache_key(
            ["BadgeUser", "cached_badgeinstances", user_id]
        )

    def upload_to_credentials_registry(self):
        fabric_gateway_url = getattr(settings, 'FABRIC_GATEWAY_URL')
        chaincode = getattr(settings, 'CREDENTIAL_CHAINCODE')
        
        json = self.get_json()

        json.pop('proof', None)

        canonicalized_credential = jsonld.normalize(
            json,
            {
                'algorithm': 'URDNA2015',
                'format': 'application/n-quads'
            }
        )

        # TODO: should we just have the hashed credential ? or should we also include proof without proof value?
        hashed_credential = hashlib.sha256(canonicalized_credential.encode("utf-8")).digest()

        issuanceDict = {
            'credentialId': json['id'],
            # TODO: should also include salt if it is hashed
            'issuedTo': json['credentialSubject']['identifier']['identityHash'],
            'issuedAt': json['validFrom']
        }

        response = requests.post(
            url = f"{fabric_gateway_url}/submit",
            headers = {"Content-Type": "application/json"},
            json = {"chaincode": chaincode, "transaction": "RegisterCredentialCommitment", "args": [
                hashed_credential.hex(),
                json['issuer'],
                issuanceDict
            ]}
        )

        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get("ok"):
                logger.logger.info(f"Credential {self.entity_id} successfully registered on credential registry.")
            else:
                raise ValidationError(f"Fabric rejected submit: {response.text}")
        else:
            logger.logger.error(f"HTTP error: {response.status_code} {response.text}")
            raise ValidationError("Could not register credential on credential registry.")

    def save(self, *args, **kwargs):
        if self.pk is None:
            # First check if recipient is in the blacklist
            if blacklist.api_query_is_in_blacklist(self.recipient_type, self.recipient_identifier):
                logger.event(badgrlog.BlacklistAssertionNotCreatedEvent(self))
                raise ValidationError("You may not award this badge to this recipient.")

            self.salt = uuid.uuid4().hex
            self.created_at = datetime.datetime.now()

            # do this now instead of in AbstractVersionedEntity.save() so we can use it for image name
            if self.entity_id is None:
                self.entity_id = generate_entity_uri()

            if not self.image:
                badgeclass_name, ext = os.path.splitext(self.badgeclass.image.file.name)
                new_image = io.BytesIO()
                bake(image_file=self.cached_badgeclass.image.file,
                     assertion_json_string=json_dumps(self.get_json(obi_version=UNVERSIONED_BAKED_VERSION), indent=2),
                     output_file=new_image)
                self.image.save(name='assertion-{id}{ext}'.format(id=self.entity_id, ext=ext),
                                content=ContentFile(new_image.read()),
                                save=False)

            with transaction.atomic():
                try:
                    super(BadgeInstance, self).save(*args, **kwargs)

                    try:
                        from badgeuser.models import CachedEmailAddress
                        existing_email = CachedEmailAddress.cached.get(email=self.recipient_identifier)
                        if self.recipient_identifier != existing_email.email and \
                                self.recipient_identifier not in [e.email for e in existing_email.cached_variants()]:
                            existing_email.add_variant(self.recipient_identifier)
                    except CachedEmailAddress.DoesNotExist:
                        pass

                    #self.upload_to_credentials_registry()
                except Exception as e:
                    cache.delete(self._badge_user_cache_key_cached_badgeinstances(self.user_id))
                    raise e
        else:
            if self.revoked is False:
                self.revocation_reason = None

            super(BadgeInstance, self).save(*args, **kwargs)

    def rebake(self, obi_version=CURRENT_OBI_VERSION, save=True):
        new_image = io.BytesIO()
        bake(
            image_file=self.cached_badgeclass.image.file,
            assertion_json_string=json_dumps(self.get_json(obi_version=obi_version), indent=2),
            output_file=new_image
        )

        new_filename = generate_rebaked_filename(self.image.name, self.cached_badgeclass.image.name)
        new_name = default_storage.save(new_filename, ContentFile(new_image.read()))
        default_storage.delete(self.image.name)
        self.image.name = new_name
        if save:
            self.save()

    def publish(self):
        fields_cache = self._state.fields_cache  # stash the fields cache to avoid publishing related objects here
        self._state.fields_cache = dict()

        super(BadgeInstance, self).publish()
        self.badgeclass.publish()
        if self.recipient_user:
            self.recipient_user.publish()

        # publish all collections this instance was in
        for collection in self.backpackcollection_set.all():
            collection.publish()

        self.publish_by('entity_id', 'revoked')
        self._state.fields_cache = fields_cache  # restore the stashed fields cache

    def delete(self, *args, **kwargs):
        badgeclass = self.badgeclass

        super(BadgeInstance, self).delete(*args, **kwargs)
        badgeclass.publish()
        if self.recipient_user:
            self.recipient_user.publish()
        self.publish_delete('entity_id', 'revoked')

    def revoke(self, revocation_reason):
        if self.revoked:
            raise ValidationError("Assertion is already revoked")

        if not revocation_reason:
            raise ValidationError("revocation_reason is required")

        self.revoked = True
        self.revocation_reason = revocation_reason
        self.image.delete()
        self.save()

    def notify_earner(self, badgr_app=None, renotify=False):
        """
        Sends an email notification to the badge recipient.
        """
        if self.recipient_type != RECIPIENT_TYPE_EMAIL:
            return

        try:
            EmailBlacklist.objects.get(email=self.recipient_identifier)
        except EmailBlacklist.DoesNotExist:
            # Allow sending, as this email is not blacklisted.
            pass
        else:
            logger.event(badgrlog.BlacklistEarnerNotNotifiedEvent(self))
            return

        if badgr_app is None:
            badgr_app = self.cached_issuer.cached_badgrapp
        if badgr_app is None:
            badgr_app = BadgrApp.objects.get_current(None)

        try:
            if self.issuer.image:
                issuer_image_url = self.issuer.public_url + '/image'
            else:
                issuer_image_url = None

            email_context = {
                'badge_name': self.badgeclass.name,
                'badge_id': self.entity_id,
                'badge_description': self.badgeclass.description,
                'help_email': getattr(settings, 'HELP_EMAIL', 'help@badgr.io'),
                'issuer_name': re.sub(r'[^\w\s]+', '', self.issuer.name, 0, re.I),
                'issuer_url': self.issuer.url,
                'issuer_email': self.issuer.email,
                'issuer_detail': self.issuer.public_url,
                'issuer_image_url': issuer_image_url,
                'badge_instance_url': self.public_url,
                'image_url': self.public_url + '/image?type=png',
                'download_url': self.public_url + "?action=download",
                'site_name': badgr_app.name,
                'site_url': badgr_app.signup_redirect,
                'badgr_app': badgr_app
            }
            if badgr_app.cors == 'badgr.io':
                email_context['promote_mobile'] = True
            if renotify:
                email_context['renotify'] = 'Reminder'
        except KeyError as e:
            # A property isn't stored right in json
            raise e

        template_name = 'issuer/email/notify_earner'
        try:
            from badgeuser.models import CachedEmailAddress
            CachedEmailAddress.objects.get(email=self.recipient_identifier, verified=True)
            template_name = 'issuer/email/notify_account_holder'
            email_context['site_url'] = badgr_app.ui_login_redirect
        except CachedEmailAddress.DoesNotExist:
            pass

        adapter = get_adapter()
        adapter.send_mail(template_name, self.recipient_identifier, context=email_context)

    def get_extensions_manager(self):
        return self.badgeinstanceextension_set

    @property
    def recipient_user(self):
        from badgeuser.models import CachedEmailAddress, UserRecipientIdentifier
        try:
            email_address = CachedEmailAddress.cached.get(email=self.recipient_identifier)
            if email_address.verified:
                return email_address.user
        except CachedEmailAddress.DoesNotExist:
            try:
                identifier = UserRecipientIdentifier.cached.get(identifier=self.recipient_identifier)
                if identifier.verified:
                    return identifier.user
            except UserRecipientIdentifier.DoesNotExist:
                pass
            pass
        return None

    def _signed_credential(self, unsigned_credential, issuer_did, verification_method):
        signing_service_url = getattr(settings, 'SIGNING_SERVICE_URL')
        payload = {
            "unsignedCredential": unsigned_credential,
            "issuerDid": issuer_did,
            "verificationMethod": verification_method,
            "extraDocuments": {}
        }
        response = requests.post(f'{signing_service_url}/sign', json=payload, timeout=10)

        if response.status_code != 200:
            raise ValidationError(f"Proof service error: {response.text}")

        data = response.json()
        if not data.get("ok"):
            raise ValidationError(data.get("error"))

        return data['credential']

    def get_json(self, obi_version=CURRENT_OBI_VERSION, include_extra=True, external_did_signing_url=None):
        _, ob_context_iri = get_obi_context(obi_version)
        _, credentials_context_iri = get_credentials_context('2_0')

        json = OrderedDict([
            ('@context', [credentials_context_iri, ob_context_iri]),
            ('type', ['VerifiableCredential', 'OpenBadgeCredential']),
            ('id', self.urn_id)
        ])

        achievement = self.cached_badgeclass.get_json(obi_version=obi_version, include_extra=include_extra)
        achievement.pop('@context', None)

        json['credentialSubject'] = {
            'type': 'AchievementSubject',
            'achievement': achievement
        }

        if self.hashed:
            json['credentialSubject']['identifier'] = {
                "type": "IdentityObject",
                "hashed": True,
                "identityType": self.recipient_type,
                "identityHash": generate_sha256_hashstring(self.recipient_identifier, self.salt),
            }
            if self.salt:
                json['credentialSubject']['identifier']['salt'] = self.salt
        else:
            json['credentialSubject']['identifier'] = {
                "type": "IdentityObject",
                "hashed": False,
                "identityType": self.recipient_type,
                "identityHash": self.recipient_identifier
            }

        json['issuer'] = self.cached_issuer.did_id

        # evidence
        if self.evidence_url:
            if obi_version == '2_0' or obi_version == '3_0':
                # obi v2 multiple evidence
                json['evidence'] = [e.get_json(obi_version) for e in self.cached_evidence()]

        # narrative
        if self.narrative and obi_version == '2_0':
            json['narrative'] = self.narrative

        # validFrom / validUntil
        json['validFrom'] = self.validFrom.isoformat()
        if self.validUntil:
            json['validUntil'] = self.validUntil.isoformat()

        logger.logger.info(json_dumps(json))
        json = self._signed_credential(json, self.cached_issuer.did_id, self.cached_issuer.main_verification_method)

        # pass through imported json
        if include_extra:
            extra = self.get_filtered_json()
            if extra is not None:
                for k,v in list(extra.items()):
                    if k not in json:
                        json[k] = v   

        return json

    @property
    def json(self):
        return self.get_json()

    def get_filtered_json(self, excluded_fields=('@context', 'id', 'type', 'uid', 'recipient', 'badge', 'validFrom', 'image', 'evidence', 'narrative', 'verify', 'verification')):
        filtered = super(BadgeInstance, self).get_filtered_json(excluded_fields=excluded_fields)
        # Ensure that the expires date string is in the expected ISO-85601 UTC format
        if filtered is not None and filtered.get('validUntil', None) and not str(filtered.get('validUntil')).endswith('Z'):
            filtered['validUntil'] = parse_original_datetime(filtered['validUntil'])
        return filtered

    @cachemodel.cached_method(auto_publish=True)
    def cached_evidence(self):
        return self.badgeinstanceevidence_set.all()

    @property
    def evidence_url(self):
        """Exists for compliance with ob1.x badges"""
        evidence_list = self.cached_evidence()
        if len(evidence_list) > 1:
            return self.public_url
        if len(evidence_list) == 1 and evidence_list[0].evidence_url:
            return evidence_list[0].evidence_url
        elif len(evidence_list) == 1:
            return self.public_url

    @property
    def evidence_items(self):
        """exists to cajole EvidenceItemSerializer"""
        return self.cached_evidence()

    @evidence_items.setter
    def evidence_items(self, value):
        def _key(narrative, url):
            return '{}-{}'.format(narrative or '', url or '')
        existing_evidence_idx = {_key(e.narrative, e.evidence_url): e for e in self.evidence_items}
        new_evidence_idx = {_key(v.get('narrative', None), v.get('evidence_url', None)): v for v in value}

        with transaction.atomic():
            if not self.pk:
                self.save()

            # add missing
            for evidence_data in value:
                key = _key(evidence_data.get('narrative', None), evidence_data.get('evidence_url', None))
                if key not in existing_evidence_idx:
                    evidence_record, created = BadgeInstanceEvidence.cached.get_or_create(
                        badgeinstance=self,
                        narrative=evidence_data.get('narrative', None),
                        evidence_url=evidence_data.get('evidence_url', None)
                    )

            # remove old
            for evidence_record in self.evidence_items:
                key = _key(evidence_record.narrative or None, evidence_record.evidence_url or None)
                if key not in new_evidence_idx:
                    evidence_record.delete()

    @property
    def cached_badgrapp(self):
        return self.cached_issuer.cached_badgrapp

    def get_baked_image_url(self, obi_version=CURRENT_OBI_VERSION):
        if obi_version == UNVERSIONED_BAKED_VERSION:
            # requested version is the one referenced in assertion.image
            return self.image.url

        try:
            baked_image = BadgeInstanceBakedImage.cached.get(badgeinstance=self, obi_version=obi_version)
        except BadgeInstanceBakedImage.DoesNotExist:
            # rebake
            baked_image = BadgeInstanceBakedImage(badgeinstance=self, obi_version=obi_version)

            json_to_bake = self.get_json(
                obi_version=obi_version,
                include_extra=True
            )
            badgeclass_name, ext = os.path.splitext(self.badgeclass.image.file.name)
            new_image = io.BytesIO()
            bake(image_file=self.cached_badgeclass.image.file,
                 assertion_json_string=json_dumps(json_to_bake, indent=2),
                 output_file=new_image)
            baked_image.image.save(
                name='assertion-{id}-{version}{ext}'.format(id=self.entity_id, ext=ext, version=obi_version),
                content=ContentFile(new_image.read()),
                save=False
            )
            baked_image.save()

        return baked_image.image.url


def _baked_badge_instance_filename_generator(instance, filename):
    return "baked/{version}/{filename}".format(
        version=instance.obi_version,
        filename=filename
    )


class BadgeInstanceBakedImage(cachemodel.CacheModel):
    badgeinstance = models.ForeignKey('issuer.BadgeInstance',
                                      on_delete=models.CASCADE)
    obi_version = models.CharField(max_length=254)
    image = models.FileField(upload_to=_baked_badge_instance_filename_generator, blank=True)

    def publish(self):
        self.publish_by('badgeinstance', 'obi_version')
        return super(BadgeInstanceBakedImage, self).publish()

    def delete(self, *args, **kwargs):
        self.publish_delete('badgeinstance', 'obi_version')
        return super(BadgeInstanceBakedImage, self).delete(*args, **kwargs)


class BadgeInstanceEvidence(OriginalJsonMixin, cachemodel.CacheModel):
    badgeinstance = models.ForeignKey('issuer.BadgeInstance',
                                      on_delete=models.CASCADE)
    evidence_url = models.CharField(max_length=2083, blank=True, null=True, default=None)
    narrative = models.TextField(blank=True, null=True, default=None)

    objects = BadgeInstanceEvidenceManager()

    def publish(self):
        super(BadgeInstanceEvidence, self).publish()
        self.badgeinstance.publish()

    def delete(self, *args, **kwargs):
        badgeinstance = self.badgeinstance
        ret = super(BadgeInstanceEvidence, self).delete(*args, **kwargs)
        badgeinstance.publish()
        return ret

    def get_json(self, obi_version=CURRENT_OBI_VERSION, include_context=False):
        json = OrderedDict()
        if include_context:
            obi_version, context_iri = get_obi_context(obi_version)
            json['@context'] = context_iri

        json['type'] = 'Evidence'
        if self.evidence_url:
            json['id'] = self.evidence_url
        if self.narrative:
            json['narrative'] = self.narrative
        return json


class BadgeClassAlignment(OriginalJsonMixin, cachemodel.CacheModel):
    badgeclass = models.ForeignKey('issuer.BadgeClass',
                                   on_delete=models.CASCADE)
    target_name = models.TextField()
    target_url = models.CharField(max_length=2083)
    target_description = models.TextField(blank=True, null=True, default=None)
    target_framework = models.TextField(blank=True, null=True, default=None)
    target_code = models.TextField(blank=True, null=True, default=None)

    def publish(self):
        super(BadgeClassAlignment, self).publish()
        self.badgeclass.publish()

    def delete(self, *args, **kwargs):
        super(BadgeClassAlignment, self).delete(*args, **kwargs)
        self.badgeclass.publish()

    def get_json(self, obi_version=CURRENT_OBI_VERSION, include_context=False):
        json = OrderedDict()
        if include_context:
            obi_version, context_iri = get_obi_context(obi_version)
            json['@context'] = context_iri

        json['targetName'] = self.target_name
        json['targetUrl'] = self.target_url
        if self.target_description:
            json['targetDescription'] = self.target_description
        if self.target_framework:
            json['targetFramework'] = self.target_framework
        if self.target_code:
            json['targetCode'] = self.target_code

        return json


class BadgeClassTag(cachemodel.CacheModel):
    badgeclass = models.ForeignKey('issuer.BadgeClass',
                                   on_delete=models.CASCADE)
    name = models.CharField(max_length=254, db_index=True)

    def __str__(self):
        return self.name

    def publish(self):
        super(BadgeClassTag, self).publish()
        self.badgeclass.publish()

    def delete(self, *args, **kwargs):
        super(BadgeClassTag, self).delete(*args, **kwargs)
        self.badgeclass.publish()


class IssuerExtension(BaseOpenBadgeExtension):
    issuer = models.ForeignKey('issuer.Issuer',
                               on_delete=models.CASCADE)

    def publish(self):
        super(IssuerExtension, self).publish()
        self.issuer.publish(publish_staff=False)

    def delete(self, *args, **kwargs):
        super(IssuerExtension, self).delete(*args, **kwargs)
        self.issuer.publish(publish_staff=False)


class BadgeClassExtension(BaseOpenBadgeExtension):
    badgeclass = models.ForeignKey('issuer.BadgeClass',
                                   on_delete=models.CASCADE)

    def publish(self):
        super(BadgeClassExtension, self).publish()
        self.badgeclass.publish()

    def delete(self, *args, **kwargs):
        super(BadgeClassExtension, self).delete(*args, **kwargs)
        self.badgeclass.publish()


class BadgeInstanceExtension(BaseOpenBadgeExtension):
    badgeinstance = models.ForeignKey('issuer.BadgeInstance',
                                      on_delete=models.CASCADE)

    def publish(self):
        super(BadgeInstanceExtension, self).publish()
        self.badgeinstance.publish()

    def delete(self, *args, **kwargs):
        super(BadgeInstanceExtension, self).delete(*args, **kwargs)
        self.badgeinstance.publish()
