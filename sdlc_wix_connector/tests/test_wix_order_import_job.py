import json

from odoo.tests.common import TransactionCase, tagged

from .test_wix_jwt import generate_keypair


def build_claims(order_id='wix-order-1', sku='WIX-SKU-1', qty=2, price='25.00', number=1001,
                  address=None, email='buyer@example.com'):
    recipient_info = {
        'contactDetails': {
            'firstName': 'Test',
            'lastName': 'Buyer',
            'phone': '+201000000000',
        },
    }
    if address is not None:
        recipient_info['address'] = address
    entity = {
        'id': order_id,
        'number': number,
        'currency': 'EGP',
        'buyerInfo': {'email': email},
        'recipientInfo': recipient_info,
        'lineItems': [{
            'quantity': qty,
            'physicalProperties': {'sku': sku},
            'price': {'amount': price},
        }],
    }
    inner = {
        'entityId': order_id,
        'createdEvent': {'entity': entity},
    }
    return {
        'data': json.dumps({
            'eventType': 'OrderCreated',
            'entityFqdn': 'wix.ecom.v1.order',
            'slug': 'created',
            'id': f'event-for-{order_id}',
            'data': json.dumps(inner),
        }),
    }


@tagged('post_install', '-at_install')
class TestWixOrderImportJob(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _, public_pem = generate_keypair()
        cls.warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1,
        )
        cls.instance = cls.env['wix.instance'].create({
            'name': 'Test Wix Store',
            'webhook_public_key': public_pem,
            'order_import_enabled': True,
            'default_warehouse_id': cls.warehouse.id,
        })
        cls.product = cls.env['product.product'].create({
            'name': 'Wix Test Product',
            'default_code': 'WIX-SKU-1',
            'is_storable': True,
            'list_price': 25.0,
        })

    def _create_job(self, claims, order_id='wix-order-1'):
        return self.env['wix.order.import.job'].create({
            'instance_id': self.instance.id,
            'payload_json': json.dumps(claims),
            'wix_order_id': order_id,
        })

    def test_job_creates_confirmed_sale_order(self):
        job = self._create_job(build_claims())
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        self.assertEqual(job.state, 'done')
        order = job.sale_order_id
        self.assertTrue(order)
        self.assertEqual(order.state, 'sale')
        self.assertEqual(order.partner_id.email, 'buyer@example.com')
        self.assertEqual(len(order.order_line), 1)
        self.assertEqual(order.order_line.product_id, self.product)
        self.assertEqual(order.order_line.product_uom_qty, 2)
        self.assertEqual(order.order_line.price_unit, 25.0)
        self.assertEqual(order.warehouse_id, self.warehouse)

    def test_new_customer_gets_address_from_wix_payload(self):
        """The address the buyer typed on Wix must land on the new contact."""
        address = {
            'addressLine1': '12 Nile St',
            'addressLine2': 'Apt 4',
            'city': 'Cairo',
            'postalCode': '11511',
            'country': 'EG',
        }
        job = self._create_job(build_claims(address=address))
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        partner = job.sale_order_id.partner_id
        self.assertEqual(partner.street, '12 Nile St')
        self.assertEqual(partner.street2, 'Apt 4')
        self.assertEqual(partner.city, 'Cairo')
        self.assertEqual(partner.zip, '11511')
        self.assertEqual(partner.country_id.code, 'EG')

    def test_street_address_form_is_also_accepted(self):
        """Wix's alternate streetAddress{name,number} shape must work too."""
        address = {
            'streetAddress': {'name': 'Nile St', 'number': '12'},
            'city': 'Cairo',
        }
        job = self._create_job(build_claims(address=address))
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        self.assertEqual(job.sale_order_id.partner_id.street, '12 Nile St')

    def test_existing_contact_address_is_not_overwritten(self):
        """A repeat Wix order must never clobber a manually-curated address."""
        self.env['res.partner'].create({
            'name': 'Test Buyer',
            'email': 'buyer@example.com',
            'street': 'Manually Entered Street',
        })
        address = {'addressLine1': 'From Wix Order', 'city': 'Alexandria'}
        job = self._create_job(build_claims(address=address))
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        partner = job.sale_order_id.partner_id
        self.assertEqual(partner.street, 'Manually Entered Street')
        # The blank city field is still filled in from the order.
        self.assertEqual(partner.city, 'Alexandria')

    def test_default_carrier_is_set_on_the_imported_order(self):
        """The connection's configured carrier must land on every import.

        Wix's own shipping method name is deliberately not mapped
        automatically (see architecture_and_decisions.md) -- only this
        explicit default is.
        """
        delivery_product = self.env['product.product'].create({
            'name': 'Test Delivery Fee',
            'type': 'service',
        })
        carrier = self.env['delivery.carrier'].create({
            'name': 'Test Carrier',
            'product_id': delivery_product.id,
        })
        self.instance.sudo().write({'default_carrier_id': carrier.id})

        job = self._create_job(build_claims(order_id='wix-order-carrier'))
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        self.assertEqual(job.sale_order_id.carrier_id, carrier)

    def test_confirmed_order_creates_standard_delivery(self):
        """Confirmation must produce a normal outgoing picking, not custom stock moves."""
        job = self._create_job(build_claims())
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        pickings = job.sale_order_id.picking_ids
        self.assertEqual(len(pickings), 1)
        self.assertEqual(pickings.picking_type_id.code, 'outgoing')
        self.assertNotIn(pickings.state, ('done', 'cancel'))

    def test_missing_sku_mapping_fails_without_creating_order(self):
        job = self._create_job(build_claims(sku='SKU-THAT-DOES-NOT-EXIST'))
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        self.assertFalse(job.sale_order_id)
        self.assertTrue(job.last_error_message)
        self.assertIn('SKU-THAT-DOES-NOT-EXIST', job.last_error_message)
        self.assertEqual(
            self.env['sale.order'].search_count([('origin', 'like', 'Wix')]), 0,
        )

    def test_duplicate_order_id_is_idempotent(self):
        """A redelivered webhook must not create a second sale order."""
        job = self._create_job(build_claims())
        self.env['wix.order.import.job'].cron_process_order_import_jobs()
        first_order = job.sale_order_id
        self.assertTrue(first_order)

        # Simulate a redelivery that bypassed the controller's dedupe.
        duplicate = self.env['wix.order.import.job'].create({
            'instance_id': self.instance.id,
            'payload_json': json.dumps(build_claims()),
            'wix_order_id': False,
        })
        duplicate.sudo().write({'wix_order_id': 'wix-order-1-redelivery'})
        duplicate.payload_json = json.dumps(build_claims())
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        self.assertEqual(duplicate.sale_order_id, first_order)
        self.assertEqual(duplicate.state, 'done')
        self.assertEqual(
            self.env['sale.order'].search_count([('id', '=', first_order.id)]), 1,
        )

    def test_same_event_id_cannot_be_queued_twice(self):
        """Deduplication is on the event id, not the order.

        An order legitimately emits several events, so the constraint must
        allow a second event for the same order while blocking a redelivery
        of the same one.
        """
        self.env['wix.order.import.job'].create({
            'instance_id': self.instance.id,
            'payload_json': json.dumps(build_claims()),
            'wix_order_id': 'wix-order-1',
            'wix_event_id': 'event-abc',
        })
        with self.assertRaises(Exception):
            with self.env.cr.savepoint():
                self.env['wix.order.import.job'].create({
                    'instance_id': self.instance.id,
                    'payload_json': json.dumps(build_claims()),
                    'wix_order_id': 'wix-order-1',
                    'wix_event_id': 'event-abc',
                })

    def test_retry_is_bounded_then_job_fails(self):
        job = self._create_job(build_claims(sku='MISSING-SKU'))
        for _attempt in range(6):
            job.sudo().write({'next_run_at': False})
            self.env['wix.order.import.job'].cron_process_order_import_jobs()
        self.assertEqual(job.state, 'failed')
        self.assertFalse(job.next_run_at)

    def test_disabled_import_defers_without_creating_order(self):
        self.instance.sudo().write({
            'order_import_enabled': False,
        })
        job = self._create_job(build_claims())
        self.env['wix.order.import.job'].cron_process_order_import_jobs()

        self.assertEqual(job.state, 'draft')
        self.assertFalse(job.sale_order_id)
        self.assertTrue(job.next_run_at)

    def test_empty_line_items_fails_cleanly(self):
        claims = build_claims()
        inner = json.loads(json.loads(claims['data'])['data'])
        inner['createdEvent']['entity']['lineItems'] = []
        envelope = json.loads(claims['data'])
        envelope['data'] = json.dumps(inner)
        job = self._create_job({'data': json.dumps(envelope)}, order_id='wix-order-empty')

        self.env['wix.order.import.job'].cron_process_order_import_jobs()
        self.assertFalse(job.sale_order_id)
        self.assertTrue(job.last_error_message)

    def test_action_retry_requeues_failed_job(self):
        job = self._create_job(build_claims(sku='MISSING-SKU'))
        job.sudo().write({'state': 'failed', 'retry_count': 5})
        job.action_retry()
        self.assertEqual(job.state, 'draft')
        self.assertEqual(job.retry_count, 0)
