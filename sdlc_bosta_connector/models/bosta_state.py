"""Bosta shipment state codes and their mapping onto this module's states.

Codes are taken from Bosta's official webhook documentation
(https://docs.bosta.co/docs/how-to/get-delivery-status-via-webhook/). The
webhook sends ``state`` as a **number**, while ``type`` comes back as a
**string** - the two are not symmetric with the create request, where ``type``
is a number.

Unknown codes are deliberately not guessed: they are recorded and left for
review rather than being folded into a nearby state.
"""

# Numeric state codes documented for the status webhook.
STATE_PICKUP_REQUESTED = 10
STATE_ROUTE_ASSIGNED = 20
STATE_PICKED_UP = 21
STATE_PICKED_UP_ALT = 23
STATE_RECEIVED_AT_WAREHOUSE = 24
STATE_IN_TRANSIT = 30
STATE_PICKED_UP_FROM_WAREHOUSE = 41
STATE_DELIVERED = 45
STATE_EXCEPTION = 47
STATE_TERMINATED = 48
STATE_CANCELED = 49
STATE_LOST = 100
STATE_DAMAGED = 101

STATE_LABELS = {
    STATE_PICKUP_REQUESTED: 'Pickup requested',
    STATE_ROUTE_ASSIGNED: 'Route assigned',
    STATE_PICKED_UP: 'Picked up',
    STATE_PICKED_UP_ALT: 'Picked up',
    STATE_RECEIVED_AT_WAREHOUSE: 'Received at warehouse',
    STATE_IN_TRANSIT: 'In transit',
    STATE_PICKED_UP_FROM_WAREHOUSE: 'Picked up from warehouse',
    STATE_DELIVERED: 'Delivered',
    STATE_EXCEPTION: 'Exception',
    STATE_TERMINATED: 'Terminated',
    STATE_CANCELED: 'Canceled',
    STATE_LOST: 'Lost',
    STATE_DAMAGED: 'Damaged',
}

# Mapping onto stock.picking.bosta_shipment_state.
STATE_TO_SHIPMENT_STATE = {
    STATE_PICKUP_REQUESTED: 'booked',
    STATE_ROUTE_ASSIGNED: 'booked',
    STATE_PICKED_UP: 'in_transit',
    STATE_PICKED_UP_ALT: 'in_transit',
    STATE_RECEIVED_AT_WAREHOUSE: 'in_transit',
    STATE_IN_TRANSIT: 'in_transit',
    STATE_PICKED_UP_FROM_WAREHOUSE: 'in_transit',
    STATE_DELIVERED: 'delivered',
    STATE_EXCEPTION: 'exception',
    STATE_TERMINATED: 'returned',
    STATE_CANCELED: 'canceled',
    STATE_LOST: 'exception',
    STATE_DAMAGED: 'exception',
}

# States where the parcel is coming back and stock must be received again.
# `Terminated` is Bosta's terminal state for a shipment that ends without
# delivery, which is the case the client described as a refused parcel.
RETURN_STATES = frozenset({STATE_TERMINATED})

# Terminal states: no further status change is expected, so polling can stop.
TERMINAL_STATES = frozenset({
    STATE_DELIVERED, STATE_TERMINATED, STATE_CANCELED,
    STATE_LOST, STATE_DAMAGED,
})


def label_for(code):
    """Human-readable label for a state code, without inventing one."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return 'Unknown'
    return STATE_LABELS.get(code, f'Unknown ({code})')


def shipment_state_for(code):
    """Return the mapped shipment state, or ``None`` for an unknown code."""
    try:
        code = int(code)
    except (TypeError, ValueError):
        return None
    return STATE_TO_SHIPMENT_STATE.get(code)


def is_return(code):
    try:
        return int(code) in RETURN_STATES
    except (TypeError, ValueError):
        return False


def is_terminal(code):
    try:
        return int(code) in TERMINAL_STATES
    except (TypeError, ValueError):
        return False
