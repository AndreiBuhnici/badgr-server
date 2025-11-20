from urllib.parse import urlparse

from django.apps import apps
from django.conf import settings
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.contrib.auth import get_user_model

from mainsite.models import AccessTokenScope
from mainsite.utils import netloc_to_domain

CorsModel = apps.get_model(getattr(settings, 'BADGR_CORS_MODEL'))
User = get_user_model()

def handle_token_save(sender, instance=None, **kwargs):
    for s in instance.scope.split():
        AccessTokenScope.objects.get_or_create(token=instance, scope=s)


def cors_allowed_sites(sender, request, **kwargs):
    origin = netloc_to_domain(urlparse(request.META['HTTP_ORIGIN']).netloc)
    return CorsModel.objects.filter(cors=origin).exists()

def get_effective_permissions(user):
    return set(p.codename if hasattr(p, 'codename') else p for p in user.get_all_permissions())

@receiver(m2m_changed, sender=User.groups.through)
def user_groups_changed(sender, instance, action, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    new_perms = get_effective_permissions(instance)

    if action == "post_add":
        if 'issuer.add_issuer' not in new_perms:
            return  
        #invalidate old token

    elif action in ("post_remove", "post_clear"):
        if 'issuer.add_issuer' in new_perms:
            return
        #invalidate old token


@receiver(m2m_changed, sender=User.user_permissions.through)
def user_permissions_changed(sender, instance, action, **kwargs):
    if action not in ("post_add", "post_remove", "post_clear"):
        return

    new_perms = get_effective_permissions(instance)

    if action == "post_add":
        if 'issuer.add_issuer' not in new_perms:
            return  
        #invalidate old token

    elif action in ("post_remove", "post_clear"):
        if 'issuer.add_issuer' in new_perms:
            return
        #invalidate old token