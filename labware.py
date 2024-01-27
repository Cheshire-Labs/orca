from main import IRoboticArm, Method


from typing import List


class Labware:
    @property
    def location(self) -> str:
        return self._location

    @property
    def name(self) -> str:
        return self._name

    def __init__(self, name: str, labware_type: str):
        self._workflow: List[Method] = []
        self._robot = None
        self._name = name
        self._labware_type = None
        self._location = None

    def set_robot(self, robot: IRoboticArm):
        self._robot = robot

    def set_workflow(self, workflow: List[Method]):
        self._workflow = workflow

    def send_to(self, location: str) -> None:
        self._robot.set_plate_type(self._labware_type)
        self._robot.pick(self._location)
        self._robot.place(self._location)
        self._location = location

    def plan_actions(self) -> None:
        for method in self._workflow:
            for action in method.actions:
                if self.name not in action.resource.input_plates: continue

                if self.location != action.resource.plate_pad:
                    robot_pick_action = RobotPickAction(self._robot, self, self._location)
                    robot_place_action = RobotPlaceAction(self._robot, self, action.resource.plate_plad)
                    self._planned_actions.append(robot_pick_action)
                    self._planned_actions.append(robot_place_action)

                if len(action.resource.input_plates) > 1:
                    for plate in action.resource.input_plates:
                        if plate != self.name:
                            self._planned_actions.append(AwaitPlateAction(plate))
                resource_action = ResourceActionFactory.create_action(action.resource, action.command, action.options)
                self._planned_actions.append(resource_action)