"""
Images per auto-label worker run (Settings -> Auto-Labeling).

The auto-label queue hands each run of DdaAutolabelWorker
(dda_autolabel_worker.py) up to 5 records, one image each, and the run
labels them one after the other within its 300 s timeout. A PortalAdmin
can save a lower number of images per run: the worker then labels the
first N records of its batch and sends the others back to the queue for
another run. Nothing saved means every record of the batch, as before
the setting existed.

The setting is one item of the portal settings table:

    {setting_key: 'autolabel.images_per_run', value: <whole number 1-5>,
     updated_at: <ms>, updated_by: <user id>}

No item means nothing saved.

Imported directly by the Settings API (data_accounts.py), which
validates and stores the value, and by the worker
(dda_autolabel_worker.py), which reads it at the start of every run.
Both are in the backend/functions bundle. The module imports no boto3,
so importing it creates no AWS clients.
"""
from decimal import Decimal
from typing import Any, List, Optional

# Settings-table key of the item.
IMAGES_PER_RUN_SETTING_KEY = 'autolabel.images_per_run'

MIN_IMAGES_PER_RUN = 1
# Equals the batchSize of DdaAutolabelWorker's SqsEventSource in
# infrastructure/lib/compute-stack.ts, so saving 5 is the same as nothing
# saved.
MAX_IMAGES_PER_RUN = 5
DEFAULT_IMAGES_PER_RUN = MAX_IMAGES_PER_RUN

IMAGES_PER_RUN_ERROR = (
    f'images_per_run must be a whole number from {MIN_IMAGES_PER_RUN} to '
    f'{MAX_IMAGES_PER_RUN}, or null to use the default '
    f'({DEFAULT_IMAGES_PER_RUN})')


def _is_allowed_int(value: Any) -> bool:
    """A non-bool int from MIN_IMAGES_PER_RUN to MAX_IMAGES_PER_RUN."""
    return (isinstance(value, int) and not isinstance(value, bool)
            and MIN_IMAGES_PER_RUN <= value <= MAX_IMAGES_PER_RUN)


def validate_images_per_run(value: Any) -> List[str]:
    """Errors for a submitted images_per_run; [] when it may be saved.

    None (use the default) and a whole number from 1 to 5 are valid.
    Anything else is [IMAGES_PER_RUN_ERROR], including a bool, a float
    such as 3.0 and a string such as '3'.
    """
    if value is None or _is_allowed_int(value):
        return []
    return [IMAGES_PER_RUN_ERROR]


def parse_stored_images_per_run(value: Any) -> Optional[int]:
    """The stored images per run as an int, or None to use the default.

    DynamoDB returns numbers as Decimal, so a finite Decimal equal to its
    integral value counts as a whole number, as does a non-bool int. A
    whole number from 1 to 5 is returned as an int; any other value,
    None included, gives None, the same as nothing saved.
    """
    if isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            return None
        value = int(value)
    if _is_allowed_int(value):
        return value
    return None
