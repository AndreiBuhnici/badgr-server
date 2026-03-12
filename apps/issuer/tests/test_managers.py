# encoding: utf-8

import os
import responses
import mock

from backpack.tests.utils import CURRENT_DIRECTORY as BACKPACK_TESTS_DIRECTORY
from issuer.models import Issuer, BadgeClass, BadgeInstance, BadgeInstanceEvidence

from mainsite.tests import BadgrTestCase, Ob3Generators, SetupIssuerHelper


def _register_image_mock(url):
    responses.add(
        responses.GET, url,
        body=open(os.path.join(BACKPACK_TESTS_DIRECTORY, 'testfiles/unbaked_image.png'), 'rb').read(),
        status=200, content_type='image/png'
    )


class BadgeInstanceAndEvidenceManagerTests(SetupIssuerHelper, BadgrTestCase, Ob3Generators):
    def setUp(self):
        super(BadgeInstanceAndEvidenceManagerTests, self).setUp()
        self.local_owner_user = self.setup_user(authenticate=False)
        self.local_issuer = self.setup_issuer(owner=self.local_owner_user)
        random_unrelated_badgeclass = self.setup_badgeclass(issuer=self.local_issuer)

    @responses.activate
    def test_update_from_ob3_basic(self):
        recipient = self.setup_user(email='recipient1@example.org')

        issuer_ob3 = self.generate_issuer_obo3()
        badgeclass_ob3 = self.generate_badgeclass_ob3()
        assertion_ob3 = self.generate_assertion_ob3()
        _register_image_mock(badgeclass_ob3['image'])

        issuer_image = Issuer.objects.image_from_ob3(issuer_ob3)
        badgeclass_image = BadgeClass.objects.image_from_ob3(badgeclass_ob3)
        badgeinstance_image = BadgeInstance.objects.image_from_ob3(badgeclass_image, assertion_ob3)

        issuer, _ = Issuer.objects.get_or_create_from_ob3(issuer_ob3, image=issuer_image)
        badgeclass, _ = BadgeClass.objects.get_or_create_from_ob3(issuer, badgeclass_ob3, image=badgeclass_image)
        with mock.patch('mainsite.blacklist.api_query_is_in_blacklist',
                        new=lambda a, b: False):
            badgeinstance, _ = BadgeInstance.objects.get_or_create_from_ob3(
                badgeclass, issuer, assertion_ob3, recipient_identifier='test@example.com', image=badgeinstance_image
            )
        self.assertTrue(badgeinstance.badgeclass, badgeclass)

        # Add evidence item that didn't exist at initial import
        assertion_ob3['evidence'] = {'id': 'https://example.com/evidence/1'}
        updated, _ = BadgeInstance.objects.update_from_ob3(
            badgeclass, issuer, assertion_ob3, recipient_identifier=badgeinstance.recipient_identifier
        )
        self.assertEqual(updated.pk, badgeinstance.pk)
        self.assertEqual(BadgeInstanceEvidence.objects.count(), 1)
        self.assertEqual(updated.cached_evidence().count(), 1)

        # That evidence item has now been deleted, make sure we stay up to date there.
        del assertion_ob3['evidence']
        updated, _ = BadgeInstance.objects.update_from_ob3(
            badgeclass, issuer, assertion_ob3, recipient_identifier=badgeinstance.recipient_identifier
        )
        self.assertEqual(BadgeInstanceEvidence.objects.count(), 0)

        # An evidence url gets added as a string in Open Badges 1.x style
        assertion_ob3['evidence'] = 'https://example.com/evidence/2'
        updated, _ = BadgeInstance.objects.update_from_ob3(
            badgeclass, issuer, assertion_ob3, recipient_identifier=badgeinstance.recipient_identifier
        )
        self.assertEqual(BadgeInstanceEvidence.objects.count(), 1)
        evidence_item = BadgeInstanceEvidence.objects.first()
        self.assertEqual(evidence_item.badgeinstance_id, badgeinstance.pk)
        self.assertEqual(evidence_item.evidence_url, assertion_ob3['evidence'])
        self.assertIsNone(evidence_item.narrative)

