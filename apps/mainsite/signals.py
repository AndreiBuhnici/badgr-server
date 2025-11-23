from urllib.parse import urlparse

from django.apps import apps
from django.conf import settings
from django.db.models.signals import m2m_changed
from django.dispatch import receiver
from django.contrib.auth import get_user_model

from oauth2_provider.models import AccessToken, RefreshToken

from django.contrib.auth.models import Group, Permission

from mainsite.models import AccessTokenScope
from mainsite.utils import netloc_to_domain
import badgrlog

CorsModel = apps.get_model(getattr(settings, 'BADGR_CORS_MODEL'))
User = get_user_model()
badgrlogger = badgrlog.BadgrLogger()

def handle_token_save(sender, instance=None, **kwargs):
    for s in instance.scope.split():
        AccessTokenScope.objects.get_or_create(token=instance, scope=s)


def cors_allowed_sites(sender, request, **kwargs):
    origin = netloc_to_domain(urlparse(request.META['HTTP_ORIGIN']).netloc)
    return CorsModel.objects.filter(cors=origin).exists()

def get_effective_permissions(user):
    return set(p.codename if hasattr(p, 'codename') else p for p in user.get_all_permissions())

@receiver(m2m_changed, sender=User.groups.through)
def user_groups_changed(sender, instance, action, pk_set, **kwargs):
    if action not in ("post_add", "post_remove"):
        return
    
    user_permissions = [perm.name for perm in Permission.objects.filter(user=instance).all()]

    group = Group.objects.filter(pk__in=pk_set).first()
    group_permissions  = [perm.name for perm in group.permissions.all()]

    user_groups = [g for g in Group.objects.filter(user=instance).all() if g.name != group.name]
    user_groups_permission = set()
    for user_group in user_groups:
        user_groups_permission.update([perm.name for perm in user_group.permissions.all()])

    if 'Can add issuer' in group_permissions and 'Can add issuer' not in user_permissions and 'Can add issuer' not in user_groups_permission:
        AccessToken.objects.filter(user=instance).delete()
        RefreshToken.objects.filter(user=instance).delete()

@receiver(m2m_changed, sender=User.user_permissions.through)
def user_permissions_changed(sender, instance, action, pk_set, **kwargs):
    if action not in ("post_add", "post_remove"):
        return
    
    user_groups = [g for g in Group.objects.filter(user=instance).all()]
    for user_group in user_groups:
        if 'Can add issuer' in [perm.name for perm in user_group.permissions.all()]:
            return

    if 'Can add issuer' == Permission.objects.filter(pk__in=pk_set).first().name:
        AccessToken.objects.filter(user=instance).delete()
        RefreshToken.objects.filter(user=instance).delete()