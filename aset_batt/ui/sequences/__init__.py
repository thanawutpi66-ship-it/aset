from .base import BaseSequenceMixin
from .iec_capacity import IecCapacityMixin
from .quick_scan import QuickScanMixin
from .hppc import HppcMixin
from .cycle_life import CycleLifeMixin

class SequencesMixin(BaseSequenceMixin, IecCapacityMixin, QuickScanMixin, HppcMixin, CycleLifeMixin):
    pass
from .base import en50342_capacity_conditions
