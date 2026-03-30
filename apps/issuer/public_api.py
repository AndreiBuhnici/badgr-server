import hashlib
import math
import os
import re
import io
import urllib.request, urllib.parse, urllib.error
import urllib.parse
import requests
from datetime import datetime, timezone

from pyld import jsonld
import base58
from nacl.signing import VerifyKey

import cairosvg
from PIL import Image
from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import DefaultStorage
from django.urls import resolve, reverse, Resolver404, NoReverseMatch
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import redirect, render_to_response
from django.views.generic import RedirectView
from entity.serializers import BaseSerializerV2
from rest_framework import status, permissions
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView
from json import loads as json_loads


import badgrlog
from . import utils
from backpack.models import BackpackCollection
from entity.api import VersionedObjectMixin
from mainsite.models import BadgrApp
from mainsite.utils import (OriginSetting, set_url_query_params, first_node_match, fit_image_to_height,
                            convert_svg_to_png)
from .models import Issuer, BadgeClass, BadgeInstance, IssuerEncryptionKeys
logger = badgrlog.BadgrLogger()


class SlugToEntityIdRedirectMixin(object):
    slugToEntityIdRedirect = False

    def get_entity_id_by_slug(self, slug):
        try:
            object = self.model.cached.get(slug=slug)
            return getattr(object, 'entity_id', None)
        except self.model.DoesNotExist:
            return None

    def get_slug_to_entity_id_redirect_url(self, slug):
        try:
            pattern_name = resolve(self.request.path_info).url_name
            entity_id = self.get_entity_id_by_slug(slug)
            if entity_id is None:
                raise Http404
            return reverse(pattern_name, kwargs={'entity_id': entity_id})
        except (Resolver404, NoReverseMatch):
            return None

    def get_slug_to_entity_id_redirect(self, slug):
        redirect_url = self.get_slug_to_entity_id_redirect_url(slug)
        if redirect_url is not None:
            query = self.request.META.get('QUERY_STRING', '')
            if query:
                redirect_url = "{}?{}".format(redirect_url, query)
            return redirect(redirect_url, permanent=True)
        else:
            raise Http404


class JSONComponentView(VersionedObjectMixin, APIView, SlugToEntityIdRedirectMixin):
    """
    Abstract Component Class
    """
    permission_classes = (permissions.AllowAny,)
    allow_any_unauthenticated_access = True
    authentication_classes = ()
    html_renderer_class = None
    template_name = 'public/bot_openbadge.html'

    def log(self, obj):
        pass

    def get_json(self, request, **kwargs):
        try:
            json = self.current_object.get_json(obi_version=self._get_request_obi_version(request), **kwargs)
        except ObjectDoesNotExist:
            raise Http404

        return json

    def get(self, request, **kwargs):
        try:
            self.current_object = self.get_object(request, **kwargs)
        except Http404:
            if self.slugToEntityIdRedirect:
                return self.get_slug_to_entity_id_redirect(kwargs.get('entity_id', None))
            else:
                raise

        self.log(self.current_object)

        if self.is_bot():
            # if user agent matches a known bot, return a stub html with opengraph tags
            return render_to_response(self.template_name, context=self.get_context_data())

        if self.is_requesting_html():
            return HttpResponseRedirect(redirect_to=self.get_badgrapp_redirect())

        json = self.get_json(request=request)
        return Response(json)

    def is_bot(self):
        """
        bots get an stub that contains opengraph tags
        """
        bot_useragents = getattr(settings, 'BADGR_PUBLIC_BOT_USERAGENTS', ['LinkedInBot'])
        user_agent = self.request.META.get('HTTP_USER_AGENT', '')
        if any(a in user_agent for a in bot_useragents):
            return True
        return False

    def is_wide_bot(self):
        """
        some bots prefer a wide aspect ratio for the image
        """
        bot_useragents = getattr(settings, 'BADGR_PUBLIC_BOT_USERAGENTS_WIDE', ['LinkedInBot'])
        user_agent = self.request.META.get('HTTP_USER_AGENT', '')
        if any(a in user_agent for a in bot_useragents):
            return True
        return False

    def is_requesting_html(self):
        if self.format_kwarg == 'json':
            return False

        html_accepts = ['text/html']

        http_accept = self.request.META.get('HTTP_ACCEPT', 'application/json')

        if self.is_bot() or any(a in http_accept for a in html_accepts):
            return True

        return False

    def get_badgrapp_redirect(self):
        badgrapp = self.current_object.cached_badgrapp
        badgrapp = BadgrApp.cached.get(pk=badgrapp.pk)  # ensure we have latest badgrapp information
        if not badgrapp.public_pages_redirect:
            badgrapp = BadgrApp.objects.get_current(request=None)  # use the default badgrapp

        redirect = badgrapp.public_pages_redirect
        if not redirect:
            redirect = 'https://{}/public/'.format(badgrapp.cors)
        else:
            if not redirect.endswith('/'):
                redirect += '/'

        path = self.request.path
        stripped_path = re.sub(r'^/public/', '', path)
        query_string = self.request.META.get('QUERY_STRING', None)
        ret = '{redirect}{path}{query}'.format(
            redirect=redirect,
            path=stripped_path,
            query='?'+query_string if query_string else '')
        return ret

    @staticmethod
    def _get_request_obi_version(request):
        return request.query_params.get('v', utils.CURRENT_OBI_VERSION)


class ImagePropertyDetailView(APIView, SlugToEntityIdRedirectMixin):
    permission_classes = (permissions.AllowAny,)

    def get_object(self, entity_id):
        try:
            current_object = self.model.cached.get(entity_id=entity_id)
        except self.model.DoesNotExist:
            return None
        else:
            self.log(current_object)
            return current_object

    def get(self, request, **kwargs):

        entity_id = kwargs.get('entity_id')
        current_object = self.get_object(entity_id)
        if current_object is None and self.slugToEntityIdRedirect and getattr(request, 'version', 'v1') == 'v2':
            return self.get_slug_to_entity_id_redirect(kwargs.get('entity_id', None))
        elif current_object is None:
            return Response(status=status.HTTP_404_NOT_FOUND)

        image_prop = getattr(current_object, self.prop)
        if not bool(image_prop):
            return Response(status=status.HTTP_404_NOT_FOUND)

        image_type = request.query_params.get('type', 'original')
        if image_type not in ['original', 'png']:
            raise ValidationError("invalid image type: {}".format(image_type))

        supported_fmts = {
            'square': (1, 1),
            'wide': (1.91, 1)
        }
        image_fmt = request.query_params.get('fmt', 'square').lower()
        if image_fmt not in list(supported_fmts.keys()):
            raise ValidationError("invalid image format: {}".format(image_fmt))

        image_url = image_prop.url
        filename, ext = os.path.splitext(image_prop.name)
        basename = os.path.basename(filename)
        dirname = os.path.dirname(filename)
        version_suffix = getattr(settings, 'CAIROSVG_VERSION_SUFFIX', '1')
        new_name = '{dirname}/converted{version}/{basename}{fmt_suffix}.png'.format(
            dirname=dirname,
            basename=basename,
            version=version_suffix,
            fmt_suffix="-{}".format(image_fmt) if image_fmt != 'square' else ""
        )
        storage = DefaultStorage()

        if image_type == 'original' and image_fmt == 'square':
            image_url = image_prop.url
        elif ext == '.svg':
            if not storage.exists(new_name):
                png_buf = None
                with storage.open(image_prop.name, 'rb') as input_svg:
                    if getattr(settings, 'SVG_HTTP_CONVERSION_ENABLED', False):
                        max_square = getattr(settings, 'IMAGE_FIELD_MAX_PX', 400)
                        png_buf = convert_svg_to_png(input_svg.read(), max_square, max_square)
                    # If conversion using the HTTP service fails, try falling back to python solution
                    if not png_buf:
                        png_buf = io.BytesIO()
                        input_svg.seek(0)
                        try:
                            cairosvg.svg2png(file_obj=input_svg, write_to=png_buf)
                        except IOError:
                            return redirect(storage.url(image_prop.name))  # If conversion fails, return existing file.
                    img = Image.open(png_buf)

                    img = fit_image_to_height(img, supported_fmts[image_fmt])

                    out_buf = io.BytesIO()
                    img.save(out_buf, format='png')
                    storage.save(new_name, out_buf)
            image_url = storage.url(new_name)
        else:
            if not storage.exists(new_name):
                with storage.open(image_prop.name, 'rb') as input_png:
                    out_buf = io.BytesIO()
                    # height and width set to the Height and Width of the original badge
                    img = Image.open(input_png)
                    img = fit_image_to_height(img, supported_fmts[image_fmt])

                    img.save(out_buf, format='png')
                    storage.save(new_name, out_buf)
            image_url = storage.url(new_name)

        return redirect(image_url)

class IssuerDidJson(JSONComponentView):
    permission_classes = (permissions.AllowAny,)
    model = Issuer

    def log(self, obj):
        logger.event(badgrlog.IssuerRetrievedEvent(obj, self.request))

    def get_context_data(self, **kwargs):
        image_url = "{}{}?type=png".format(
            OriginSetting.HTTP,
            reverse('issuer_image', kwargs={'entity_id': self.current_object.entity_id})
        )
        if self.is_wide_bot():
            image_url = "{}&fmt=wide".format(image_url)

        return dict(
            title=self.current_object.name,
            description=self.current_object.description,
            public_url=self.current_object.public_url,
            image_url=image_url
        )
    
    def get_json(self, request, **kwargs):
        try:
            json = self.current_object.get_json(obi_version=self._get_request_obi_version(request), **kwargs)
        except ObjectDoesNotExist:
            raise Http404

        return json


class IssuerBadgesJson(JSONComponentView):
    permission_classes = (permissions.AllowAny,)
    model = Issuer

    def log(self, obj):
        logger.event(badgrlog.IssuerBadgesRetrievedEvent(obj, self.request))

    def get_json(self, request):
        obi_version=self._get_request_obi_version(request)

        return [b.get_json(obi_version=obi_version) for b in self.current_object.cached_badgeclasses()]


class IssuerImage(ImagePropertyDetailView):
    model = Issuer
    prop = 'image'

    def log(self, obj):
        logger.event(badgrlog.IssuerImageRetrievedEvent(obj, self.request))


class BadgeClassJson(JSONComponentView):
    permission_classes = (permissions.AllowAny,)
    model = BadgeClass

    def log(self, obj):
        logger.event(badgrlog.BadgeClassRetrievedEvent(obj, self.request))

    def get_json(self, request):
        expands = request.GET.getlist('expand', [])
        json = super(BadgeClassJson, self).get_json(request)
        obi_version = self._get_request_obi_version(request)

        if 'issuer' in expands:
            json['issuer'] = self.current_object.cached_issuer.get_json(obi_version=obi_version)

        return json

    def get_context_data(self, **kwargs):
        image_url = "{}{}?type=png".format(
            OriginSetting.HTTP,
            reverse('badgeclass_image', kwargs={'entity_id': self.current_object.entity_id})
        )
        if self.is_wide_bot():
            image_url = "{}&fmt=wide".format(image_url)
        return dict(
            title=self.current_object.name,
            description=self.current_object.description,
            public_url=self.current_object.public_url,
            image_url=image_url
        )


class BadgeClassImage(ImagePropertyDetailView):
    model = BadgeClass
    prop = 'image'

    def log(self, obj):
        logger.event(badgrlog.BadgeClassImageRetrievedEvent(obj, self.request))


class BadgeClassCriteria(RedirectView, SlugToEntityIdRedirectMixin):
    permanent = False
    model = BadgeClass

    def get_redirect_url(self, *args, **kwargs):
        try:
            badge_class = self.model.cached.get(entity_id=kwargs.get('entity_id'))
        except self.model.DoesNotExist:
            if self.slugToEntityIdRedirect:
                return self.get_slug_to_entity_id_redirect_url(kwargs.get('entity_id'))
            else:
                return None
        return badge_class.get_absolute_url()


class BadgeInstanceJson(JSONComponentView):
    permission_classes = (permissions.AllowAny,)
    model = BadgeInstance

    def has_object_permissions(self, request, obj):
        if obj.pending:
            raise Http404
        return super(BadgeInstanceJson, self).has_object_permissions(request, obj)

    def get_json(self, request):
        expands = request.GET.getlist('expand', [])
        json = super(BadgeInstanceJson, self).get_json(
            request,
            external_did_signing_url=None,
            expand_issuer=('issuer' in expands)
        )

        return json

    def get_context_data(self, **kwargs):
        image_url = "{}{}?type=png".format(
            OriginSetting.HTTP,
            reverse('badgeclass_image', kwargs={'entity_id': self.current_object.cached_badgeclass.entity_id})
        )
        if self.is_wide_bot():
            image_url = "{}&fmt=wide".format(image_url)

        oembed_link_url = '{}{}?format=json&url={}'.format(
            getattr(settings, 'HTTP_ORIGIN'),
            reverse('oembed_api_endpoint'),
            urllib.parse.quote(self.current_object.public_url)
        )

        return dict(
            user_agent=self.request.META.get('HTTP_USER_AGENT', ''),
            title=self.current_object.cached_badgeclass.name,
            description=self.current_object.cached_badgeclass.description,
            public_url=self.current_object.public_url,
            image_url=image_url,
            oembed_link_url=oembed_link_url
        )


class BadgeInstanceImage(ImagePropertyDetailView):
    model = BadgeInstance
    prop = 'image'

    def log(self, badge_instance):
        logger.event(badgrlog.BadgeInstanceDownloadedEvent(badge_instance, self.request))

    def get_object(self, slug):
        obj = super(BadgeInstanceImage, self).get_object(slug)
        if obj and obj.revoked:
            return None
        return obj


class BackpackCollectionJson(JSONComponentView):
    permission_classes = (permissions.AllowAny,)
    model = BackpackCollection
    entity_id_field_name = 'share_hash'

    def get_context_data(self, **kwargs):
        image_url = ''
        if self.current_object.cached_badgeinstances().exists():
            chosen_assertion = sorted(self.current_object.cached_badgeinstances(), key=lambda b: b.issued_on)[0]
            image_url = "{}{}?type=png".format(
                OriginSetting.HTTP,
                reverse('badgeinstance_image', kwargs={'entity_id': chosen_assertion.entity_id})
            )
            if self.is_wide_bot():
                image_url = "{}&fmt=wide".format(image_url)

        return dict(
            title=self.current_object.name,
            description=self.current_object.description,
            public_url=self.current_object.share_url,
            image_url=image_url
        )

    def get_json(self, request):
        expands = request.GET.getlist('expand', [])
        if not self.current_object.published:
            raise Http404

        json = self.current_object.get_json(
            obi_version=self._get_request_obi_version(request),
            expand_badgeclass=('badges.badge' in expands),
            expand_issuer=('badges.badge.issuer' in expands)
        )
        return json


class BakedBadgeInstanceImage(VersionedObjectMixin, APIView, SlugToEntityIdRedirectMixin):
    permission_classes = (permissions.AllowAny,)
    allow_any_unauthenticated_access = True
    model = BadgeInstance

    def get(self, request, **kwargs):
        try:
            assertion = self.get_object(request, **kwargs)
        except Http404:
            if self.slugToEntityIdRedirect:
                return self.get_slug_to_entity_id_redirect(kwargs.get('entity_id', None))
            else:
                raise

        requested_version = request.query_params.get('v', utils.CURRENT_OBI_VERSION)
        if requested_version not in list(utils.OBI_VERSION_CONTEXT_IRIS.keys()):
            raise ValidationError("Invalid OpenBadges version")

        redirect_url = assertion.get_baked_image_url(obi_version=requested_version)

        return redirect(redirect_url, permanent=True)



class OEmbedAPIEndpoint(APIView):
    permission_classes = (permissions.AllowAny,)

    @staticmethod
    def get_object(url):
        request_url = urllib.parse.urlparse(url)

        try:
            resolved = resolve(request_url.path)
        except Http404:
            raise Http404("Cannot find resource.")

        if resolved.url_name == 'badgeinstance_json':
            return BadgeInstance.cached.get(entity_id=resolved.kwargs.get('entity_id'))
        raise Http404('Cannot find resource.')

    def get_badgrapp_redirect(self, entity):
        badgrapp = entity.cached_badgrapp
        if not badgrapp or not badgrapp.public_pages_redirect:
            badgrapp = BadgrApp.objects.get_current(request=None)  # use the default badgrapp

        redirect_url = badgrapp.public_pages_redirect
        if not redirect_url:
            redirect_url = 'https://{}/public/'.format(badgrapp.cors)
        else:
            if not redirect_url.endswith('/'):
                redirect_url += '/'

        path = entity.get_absolute_url()
        stripped_path = re.sub(r'^/public/', '', path)
        ret = '{redirect}{path}'.format(
            redirect=redirect_url,
            path=stripped_path)
        ret = set_url_query_params(ret, embedVersion=2)
        return ret

    def get_max_constrained_height(self, request):
        min_height = 420
        height = int(request.query_params.get('maxwidth', min_height))
        return max(min_height, height)

    def get_max_constrained_width(self, request):
        max_width = 800
        min_width = 320
        width = int(request.query_params.get('maxwidth', max_width))
        return max(min_width, min(width, max_width))

    def get(self, request, **kwargs):
        try:
            url = request.query_params.get('url')
            constrained_height = self.get_max_constrained_height(request)
            constrained_width = self.get_max_constrained_width(request)
            response_format = request.query_params.get('format', 'json')
        except (TypeError, ValueError):
            raise ValidationError("Cannot parse OEmbed request parameters.")

        if response_format != 'json':
            return Response("Only json format is supported at this time.", status=status.HTTP_501_NOT_IMPLEMENTED)

        try:
            badgeinstance = self.get_object(url)
        except BadgeInstance.DoesNotExist:
            raise Http404("Object to embed not found.")

        badgeclass = badgeinstance.cached_badgeclass
        issuer = badgeinstance.cached_issuer
        badgrapp = BadgrApp.objects.get_current(request)

        data = {
            'type': 'rich',
            'version': '1.0',
            'title': badgeclass.name,
            'author_name': issuer.name,
            'author_url': issuer.url,
            'provider_name': badgrapp.name,
            'provider_url': badgrapp.ui_login_redirect,
            'thumbnail_url': badgeinstance.image_url(),
            'thumnail_width': 200,  # TODO: get real data; respect maxwidth
            'thumbnail_height': 200,  # TODO: get real data; respect maxheight
            'width': constrained_width,
            'height': constrained_height
        }

        data['html'] = """<iframe src="{src}" frameborder="0" width="{width}px" height="{height}px"></iframe>""".format(
            src=self.get_badgrapp_redirect(badgeinstance),
            width=constrained_width,
            height=constrained_height
        )

        return Response(data, status=status.HTTP_200_OK)



class VerifyBadgeAPIEndpoint(JSONComponentView):
    permission_classes = (permissions.AllowAny,)
    @staticmethod
    def get_object(entity_id):
        try:
            return BadgeInstance.cached.get(entity_id=entity_id)

        except BadgeInstance.DoesNotExist:
            raise Http404

    def verify_issuer_registry(self, issuer_did, signing_key):
        fabric_gateway_url = getattr(settings, 'FABRIC_GATEWAY_URL')
        chaincode = getattr(settings, 'ISSUER_CHAINCODE')
        
        response = requests.post(
            url = f"{fabric_gateway_url}/evaluate",
            headers = {"Content-Type": "application/json"},
            json = {"chaincode": chaincode, "transaction": "GetIssuer", "args": [issuer_did]}
        )
        
        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get("ok"):
                logger.logger.info(f"Issuer {issuer_did} successfully retrieved from issuer registry.")

                ledger_data = json_loads(resp_json['result'])

                if ledger_data['status'] != 'authorized':
                    raise ValidationError(f"Issuer not authorized, status is {ledger_data['status']}.")

                found_key = None
                for method in ledger_data['methods']:
                    if signing_key == method['id']:
                        found_key = method

                if found_key == None:
                    raise ValidationError(f"No match for signing key in authorized verification methods.")

                now = datetime.now(timezone.utc)
                valid_from = datetime.strptime(found_key['validFrom'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

                if now < valid_from:
                    raise ValidationError(f"Invalid starting date for signing key.")

                if 'validUntil' in found_key.keys():
                    valid_until = datetime.strptime(found_key['validUntil'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if now > valid_until:
                        raise ValidationError(f"Key is expired.")
            else:
                raise ValidationError(f"Fabric rejected submit: {response.text}")
        else:
            logger.logger.error(f"HTTP error: {response.status_code} {response.text}")
            raise ValidationError("Could not retrieve issuer from issuer registry.")

    def verify_credential_registry(self, credentialHash, issuer_did):
        fabric_gateway_url = getattr(settings, 'FABRIC_GATEWAY_URL')
        chaincode = getattr(settings, 'CREDENTIAL_CHAINCODE')
        
        response = requests.post(
            url = f"{fabric_gateway_url}/evaluate",
            headers = {"Content-Type": "application/json"},
            json = {"chaincode": chaincode, "transaction": "GetCredential", "args": [credentialHash]}
        )

        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get("ok"):
                logger.logger.info(f"Credential with hash {credentialHash} successfully retrieved from issuer registry.")

                ledger_data = json_loads(resp_json['result'])

                if ledger_data['status'] != 'active':
                    raise ValidationError(f"Credential is no longer active, status is {ledger_data['status']}.")

                if ledger_data['issuerId'] != issuer_did:
                    raise ValidationError(f"Issuers do not match.")
            else:
                raise ValidationError(f"Fabric rejected submit: {response.text}")

        else:
            logger.logger.error(f"HTTP error: {response.status_code} {response.text}")
            raise ValidationError("Could not retrieve credential from credential registry.")


    def post(self, request, **kwargs):
        entity_id = request.data.get('entity_id')
        external_did = request.data.get('external_did', None)
        obi_version = self._get_request_obi_version(request)
        if obi_version == '3_0':
            entity_id = entity_id.split(':')[-1]

        badge_instance = self.get_object(entity_id)

        if obi_version == '3_0':
            # Get the json
            vc = badge_instance.get_json(obi_version=obi_version)
            
            # Remove proof options
            proof = vc.pop("proof")

            # Remove the proof value
            proof_value = proof.pop("proofValue")

            # Canonicalize the vc json and the proof options
            canonicalized_vc = jsonld.normalize(
                vc,
                {
                    'algorithm': 'URDNA2015',
                    'format': 'application/n-quads'
                }
            )

            canonicalized_proof = jsonld.normalize(
                proof,
                {
                    'algorithm': 'URDNA2015',
                    'format': 'application/n-quads'
                }
            )
            
            # Hash the canonicalized vc and proof options
            doc_hash = hashlib.sha256(canonicalized_vc.encode()).digest()
            proof_hash = hashlib.sha256(canonicalized_proof.encode()).digest()

            message = proof_hash + doc_hash

            # Extract the signed message
            signature = base58.b58decode(proof_value[1:])

            # Get the public key of the issuer
            if external_did is not None and external_did.startswith('did:web:'):
                url = utils.convert_did_web_to_url(external_did)

                # TODO: use url to download did json and extract public key
                public_key_bytes = None
            else:
                issuer_keys = badge_instance.cached_issuer.keys.filter(key_fragment=proof['verificationMethod'].split('#')[-1]).first()
                public_key_bytes = issuer_keys.get_public_key_bytes()
            
            # Check the signature
            verify_key = VerifyKey(public_key_bytes)
            try:
                verify_key.verify(message, signature)
            except Exception as e:
                raise ValidationError([{'name': "INVALID_SIGNATURE", 'description': 'Signature was forged or corrupt: {}'.format(str(e))}])

            # Check Issuer Registry
            #self.verify_issuer_registry(vc['issuer'], proof['verificationMethod'])

            # Check Credential Registry
            #self.verify_credential_registry(doc_hash.hex(), vc['issuer'])

        result = self.get_object(entity_id).get_json(expand_issuer=True)

        return Response(BaseSerializerV2.response_envelope([result], True, 'OK'), status=status.HTTP_200_OK)
