
from cheshire_drivers import HumanSim, SimTransporterDriver
from orca.resource_models.transporter import Transporter


class HumanTransfer(Transporter):
    """ A transporter that requires human interaction to pick and place labware. """

    def __init__(self,
                 name: str,
                 teachpoints: str) -> None:
        """ Initializes the HumanTransfer with a name and teachpoints file.
        Args:
            name (str): The name of the transporter.
            teachpoints (str): The file path to the teachpoints XML file.
        """
        driver = SimTransporterDriver("human_transfer", HumanSim())
        super().__init__(name, driver, teachpoints)