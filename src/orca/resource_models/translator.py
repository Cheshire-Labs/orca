from typing import ClassVar

from cheshire_drivers.interfaces import ITransporterDriver
from cheshire_drivers.translator_driver import SimTranslatorDriver
from orca.resource_models.transporter import Transporter


class Translator(Transporter):
    """A transporter whose taught positions are stations of ONE carriage.

    A translator (shuttle, bridge) is a single platform on a rail: its start
    and end are the same pad seen from two robot zones, so the whole position
    set holds at most one labware. The reservation layer needs that fact at
    topology-build time to refuse promising both endpoints at once, and it
    reads it off the bound driver.

    Declaring the machine here rather than leaving a plain ``Transporter`` and
    hoping the right driver turns up is what makes the fact survive a hosted
    deployment: the device kind selects the driver pair, so a factory that
    knows nothing about this workcell still builds a translator driver. A
    package-local factory that special-cased the name was silently replaced by
    whatever factory the host bound, and the workcell came up with two plates
    promised one carriage.
    """

    KIND: ClassVar[str] = "translator"
    DEFAULT_SIM_DRIVER: ClassVar[type[ITransporterDriver]] = SimTranslatorDriver
