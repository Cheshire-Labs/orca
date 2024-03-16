from workflow_models.workflow_templates import LabwareThreadTemplate





class Spawn384TipsScript(LabwareThreadScript):
    def __init__(self):
        super().__init__()
        self._384_tips_spawned = 0

    def on_init(self, template: LabwareThreadTemplate):
        if self._384_tips_spawned % 4 != 0:
            template.start = self._system.locations["stacker-384-tips-end"] 
        self._384_tips_spawned += 1


