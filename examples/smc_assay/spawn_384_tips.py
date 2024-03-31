from workflow_models.workflow_templates import ThreadTemplate

class Spawn384TipsScript(LabwareThreadScript):
    def __init__(self):
        super().__init__()
        self._384_tips_spawned = 0

    def on_init(self, template: ThreadTemplate):
        if self._384_tips_spawned % 4 != 0:
            template.start_location = self._system.locations["stacker-384-tips-end"] 
        self._384_tips_spawned += 1


