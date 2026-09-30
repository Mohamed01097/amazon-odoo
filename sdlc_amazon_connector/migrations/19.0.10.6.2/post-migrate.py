import logging


_logger = logging.getLogger(__name__)

# Messages that can only come from the local Ready-to-Ship validation, which runs
# before the Amazon generateTransportationOptions request is sent.
READY_TO_SHIP_ERROR_PATTERNS = (
    'Enter a Ready-to-Ship Date/Time before generating transportation options.%',
    'Ready-to-Ship Date/Time must be at least %',
)

NO_ACTIVE_GENERATION_JOB = """
    NOT EXISTS (
        SELECT 1
          FROM amazon_inbound_operation_job job
         WHERE job.inbound_shipment_id = physical.inbound_shipment_id
           AND job.operation_type = 'generate_transportation_options'
           AND job.state IN ('pending', 'in_progress')
    )
"""


def migrate(cr, version):
    """Release physical shipments locked by a pre-write transportation failure.

    Before 19.0.10.6.2 a Ready-to-Ship validation failure inside the background
    job was recorded as WRITE_OUTCOME_UNKNOWN (and older builds could leave the
    generation status at pending/in_progress). Amazon was never called in that
    case, so the record is reclassified as PRE_WRITE_FAILED / failed: the
    Ready-to-Ship field becomes editable again and Regenerate Transportation
    Options is allowed. Genuine ambiguous Amazon writes are left untouched.
    """
    if not version:
        return
    cr.execute("""
        UPDATE amazon_fba_physical_shipment physical
           SET transportation_error_code = 'PRE_WRITE_FAILED',
               transportation_generation_status = 'failed'
         WHERE COALESCE(physical.transportation_generation_operation_id, '') = ''
           AND COALESCE(physical.transportation_confirmation_operation_id, '') = ''
           AND physical.transportation_confirmation_status IS NULL
           AND physical.transportation_generation_status IN ('pending', 'in_progress', 'failed')
           AND (
                physical.transportation_error_message LIKE %s
                OR physical.transportation_error_message LIKE %s
           )
           AND {no_active_job}
     RETURNING physical.id
    """.format(no_active_job=NO_ACTIVE_GENERATION_JOB), READY_TO_SHIP_ERROR_PATTERNS)
    released = [row[0] for row in cr.fetchall()]
    if released:
        _logger.info(
            "Released %s Amazon physical shipment(s) blocked by a pre-write "
            "Ready-to-Ship validation failure: %s", len(released), released,
        )
