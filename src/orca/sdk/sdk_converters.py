
from orca.resource_models.base_resource import Equipment, ILabwarePlaceableDriver, LabwareLoadableEquipment

from orca.resource_models.labware import LabwareTemplate
from orca.resource_models.location import Location
from orca.resource_models.resource_pool import EquipmentResourcePool
from orca.sdk import sdk
from orca.system.system_map import ILocationRegistry
from orca.workflow_models.workflow_templates import MethodActionTemplate, MethodTemplate, ThreadTemplate, WorkflowTemplate


def convert_sdk_equipment_to_system_equipment(equipment: sdk.Equipment) -> Equipment:
    """
    Convert SDK equipment to system equipment.
    :param equipment: The SDK equipment to convert.
    :return: The converted system equipment.
    """
    system_equipment: Equipment
    if isinstance(equipment.driver, ILabwarePlaceableDriver):
        system_equipment = LabwareLoadableEquipment(
            name=equipment.name,
            driver=equipment.driver,
        )
    
    else:
        system_equipment = Equipment(
            name=equipment.name,
            driver=equipment.driver,
        )
    return system_equipment

def convert_sdk_equipment_pool_to_system_equipment_pool(equipment_pool: sdk.EquipmentPool) -> EquipmentResourcePool:
    """
    Convert SDK equipment pool to system equipment pool.
    :param equipment_pool: The SDK equipment pool to convert.
    :return: The converted system equipment pool.
    """
    system_equipment_pool = EquipmentResourcePool(
        name=equipment_pool.name,
        resources=[convert_sdk_equipment_to_system_equipment(r) for r in equipment_pool.resources],
    )
    return system_equipment_pool

def convert_sdk_labware_to_system_labware(labware: sdk.Labware) -> LabwareTemplate:
    """
    Convert SDK labware to system labware.
    :param labware: The SDK labware to convert.
    :return: The converted system labware.
    """
    system_labware = LabwareTemplate(
        name=labware.name,
        type=labware.type,
    )
    return system_labware

def convert_sdk_location_to_system_location(location: sdk.Location) -> Location:
    """
    Convert SDK location to system location.
    :param location: The SDK location to convert.
    :return: The converted system location.
    """
    resource = (
        convert_sdk_equipment_to_system_equipment(location.resource)
        if location.resource is not None
        else None
    )

    if resource and not isinstance(resource, LabwareLoadableEquipment):
        raise ValueError(
            f"Location {location.name} does not have a valid loadable equipment type."
        )
    assert isinstance(resource, LabwareLoadableEquipment) 
    return Location(
        teachpoint_name=location.name,
        resource=resource,
    )

def convert_sdk_action_to_system_action(action: sdk.Action) -> MethodActionTemplate:
    """
    Convert SDK action to system action.
    :param action: The SDK action to convert.
    :return: The converted system action.
    """
    system_resource: Equipment | EquipmentResourcePool
    if isinstance(action.resource, sdk.Equipment):
        system_resource = convert_sdk_equipment_to_system_equipment(action.resource)
    elif isinstance(action.resource, sdk.EquipmentPool):
        system_resource = convert_sdk_equipment_pool_to_system_equipment_pool(action.resource)
    else:
        raise ValueError(f"Unsupported resource type: {type(action.resource)}")

    system_action = MethodActionTemplate(
        resource=system_resource,
        command=action.command,
        inputs=[convert_sdk_labware_to_system_labware(l) for l in action.inputs],
        outputs=[convert_sdk_labware_to_system_labware(l) for l in action.outputs],
        options=action.options
    )
    return system_action

def convert_sdk_method_to_system_method(method: sdk.Method) -> MethodTemplate:
    """
    Convert SDK method to system method.
    :param method: The SDK method to convert.
    :return: The converted system method.
    """
    system_method = MethodTemplate(
        name=method.name,
    )
    for a in method.actions:
        system_action = convert_sdk_action_to_system_action(a)
        system_method.append_action(system_action)
    return system_method


def convert_sdk_thread_to_system_thread(thread: sdk.Thread, location_reg: ILocationRegistry) -> ThreadTemplate:
    """
    Convert SDK thread to system thread.
    :param thread: The SDK thread to convert.
    :return: The converted system thread.
    """

    thread_start = location_reg.get_location(thread.start)
    thread_end = location_reg.get_location(thread.end)
    system_thread = ThreadTemplate(
        labware_template=convert_sdk_labware_to_system_labware(thread.labware),
        start=thread_start,
        end=thread_end,
    )
    for step in thread.steps:
        system_method = convert_sdk_method_to_system_method(step)
        system_thread.add_method(system_method)

    return system_thread

def convert_sdk_workflow_to_system_workflow(workflow: sdk.Workflow, location_reg: ILocationRegistry) -> WorkflowTemplate:
    """
    Convert SDK workflow to system workflow.
    :param workflow: The SDK workflow to convert.
    :return: The converted system workflow.
    """
    system_workflow = WorkflowTemplate(
        name=workflow.name,
    )
    for t in workflow.threads:
        system_thread = convert_sdk_thread_to_system_thread(t, location_reg)
        system_workflow.add_thread(system_thread)
    return system_workflow
