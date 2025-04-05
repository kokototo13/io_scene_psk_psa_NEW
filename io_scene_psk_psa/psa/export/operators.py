import re
from collections import Counter
from typing import List, Iterable, Dict, Tuple

import bpy
from bpy.props import StringProperty
from bpy.types import Context, Armature, Action, Object, AnimData, TimelineMarker
from bpy_extras.io_utils import ExportHelper
from bpy_types import Operator

from ..builder import build_psa, PsaBuildSequence, PsaBuildOptions
from ..export.properties import PSA_PG_export, PSA_PG_export_action_list_item, filter_sequences
from ..writer import write_psa
from ...helpers import populate_bone_group_list, get_nla_strips_in_timeframe


def is_action_for_armature(armature: Armature, action: Action):
    if len(action.fcurves) == 0:
        return False
    bone_names = set([x.name for x in armature.bones])
    for fcurve in action.fcurves:
        match = re.match(r'pose\.bones\[\"([^\"]+)\"](\[\"([^\"]+)\"])?', fcurve.data_path)
        if not match:
            continue
        bone_name = match.group(1)
        if bone_name in bone_names:
            return True
    return False


def update_actions_and_timeline_markers(context: Context, armature: Armature):
    pg = getattr(context.scene, 'psa_export')

    # Clear actions and markers.
    pg.action_list.clear()
    pg.marker_list.clear()

    # Get animation data.
    animation_data_object = get_animation_data_object(context)
    animation_data = animation_data_object.animation_data if animation_data_object else None

    if animation_data is None:
        return

    # Populate actions list.
    for action in bpy.data.actions:
        if not is_action_for_armature(armature, action):
            continue

        if not action.name.startswith('#'):
            for (name, frame_start, frame_end) in get_sequences_from_action(action):
                item = pg.action_list.add()
                item.action = action
                item.name = name
                item.is_selected = False
                item.is_pose_marker = False
                item.frame_start = frame_start
                item.frame_end = frame_end

        # Pose markers are not guaranteed to be in frame-order, so make sure that they are.
        pose_markers = sorted(action.pose_markers, key=lambda x: x.frame)
        for pose_marker_index, pose_marker in enumerate(pose_markers):
            if pose_marker.name.startswith('#'):
                continue
            for (name, frame_start, frame_end) in get_sequences_from_action_pose_marker(action, pose_markers, pose_marker, pose_marker_index):
                item = pg.action_list.add()
                item.action = action
                item.name = name
                item.is_selected = False
                item.is_pose_marker = True
                item.frame_start = frame_start
                item.frame_end = frame_end

    # Populate timeline markers list.
    marker_names = [x.name for x in context.scene.timeline_markers]
    sequence_frame_ranges = get_timeline_marker_sequence_frame_ranges(animation_data, context, marker_names)

    for marker_name in marker_names:
        if marker_name not in sequence_frame_ranges:
            continue
        if marker_name.startswith('#'):
            continue
        item = pg.marker_list.add()
        item.name = marker_name
        item.is_selected = False
        frame_start, frame_end = sequence_frame_ranges[marker_name]
        item.frame_start = frame_start
        item.frame_end = frame_end


def get_sequence_fps(context: Context, fps_source: str, fps_custom: float, actions: Iterable[Action]) -> float:
    if fps_source == 'SCENE':
        return context.scene.render.fps
    elif fps_source == 'CUSTOM':
        return fps_custom
    elif fps_source == 'ACTION_METADATA':
        # Get the minimum value of action metadata FPS values.
        fps_list = []
        for action in filter(lambda x: 'psa_sequence_fps' in x, actions):
            fps = action['psa_sequence_fps']
            if type(fps) == int or type(fps) == float:
                fps_list.append(fps)
        if len(fps_list) > 0:
            return min(fps_list)
        else:
            # No valid action metadata to use, fallback to scene FPS
            return context.scene.render.fps
    else:
        raise RuntimeError(f'Invalid FPS source "{fps_source}"')


def get_animation_data_object(context: Context) -> Object:
    pg: PSA_PG_export = getattr(context.scene, 'psa_export')

    active_object = context.view_layer.objects.active

    if active_object.type != 'ARMATURE':
        raise RuntimeError('Selected object must be an Armature')

    if pg.should_override_animation_data:
        animation_data_object = pg.animation_data_override
    else:
        animation_data_object = active_object

    return animation_data_object


def is_bone_filter_mode_item_available(context, identifier):
    if identifier == 'BONE_GROUPS':
        obj = context.active_object
        if not obj.pose or not obj.pose.bone_groups:
            return False
    return True


def get_timeline_marker_sequence_frame_ranges(animation_data: AnimData, context: Context, marker_names: List[str]) -> Dict:
    # Timeline markers need to be sorted so that we can determine the sequence start and end positions.
    sequence_frame_ranges = dict()
    sorted_timeline_markers = list(sorted(context.scene.timeline_markers, key=lambda x: x.frame))
    sorted_timeline_marker_names = list(map(lambda x: x.name, sorted_timeline_markers))

    for marker_name in marker_names:
        marker = context.scene.timeline_markers[marker_name]
        frame_start = marker.frame
        # Determine the final frame of the sequence based on the next marker.
        # If no subsequent marker exists, use the maximum frame_end from all NLA strips.
        marker_index = sorted_timeline_marker_names.index(marker_name)
        next_marker_index = marker_index + 1
        frame_end = 0
        if next_marker_index < len(sorted_timeline_markers):
            # There is a next marker. Use that next marker's frame position as the last frame of this sequence.
            frame_end = sorted_timeline_markers[next_marker_index].frame
            nla_strips = get_nla_strips_in_timeframe(animation_data, marker.frame, frame_end)
            if len(nla_strips) > 0:
                frame_end = min(frame_end, max(map(lambda nla_strip: nla_strip.frame_end, nla_strips)))
                frame_start = max(frame_start, min(map(lambda nla_strip: nla_strip.frame_start, nla_strips)))
            else:
                # No strips in between this marker and the next, just export this as a one-frame animation.
                frame_end = frame_start
        else:
            # There is no next marker.
            # Find the final frame of all the NLA strips and use that as the last frame of this sequence.
            for nla_track in animation_data.nla_tracks:
                if nla_track.mute:
                    continue
                for strip in nla_track.strips:
                    frame_end = max(frame_end, strip.frame_end)

        if frame_start > frame_end:
            continue

        sequence_frame_ranges[marker_name] = int(frame_start), int(frame_end)

    return sequence_frame_ranges


def get_sequences_from_action(action: Action) -> List[Tuple[str, int, int]]:
    frame_start = int(action.frame_range[0])
    frame_end = int(action.frame_range[1])
    reversed_pattern = r'(.+)/(.+)'
    reversed_match = re.match(reversed_pattern, action.name)
    if reversed_match:
        forward_name = reversed_match.group(1)
        backwards_name = reversed_match.group(2)
        return [
            (forward_name, frame_start, frame_end),
            (backwards_name, frame_end, frame_start)
        ]
    else:
        return [(action.name, frame_start, frame_end)]


def get_sequences_from_action_pose_marker(action: Action, pose_markers: List[TimelineMarker], pose_marker: TimelineMarker, pose_marker_index: int) -> List[Tuple[str, int, int]]:
    frame_start = pose_marker.frame
    if pose_marker_index + 1 < len(pose_markers):
        frame_end = pose_markers[pose_marker_index + 1].frame
    else:
        frame_end = int(action.frame_range[1])
    reversed_pattern = r'(.+)/(.+)'
    reversed_match = re.match(reversed_pattern, pose_marker.name)
    if reversed_match:
        forward_name = reversed_match.group(1)
        backwards_name = reversed_match.group(2)
        return [
            (forward_name, frame_start, frame_end),
            (backwards_name, frame_end, frame_start)
        ]
    else:
        return [(pose_marker.name, frame_start, frame_end)]


def get_visible_sequences(pg: PSA_PG_export, sequences) -> List[PSA_PG_export_action_list_item]:
    visible_sequences = []
    for i, flag in enumerate(filter_sequences(pg, sequences)):
        if bool(flag & (1 << 30)):
            visible_sequences.append(sequences[i])
    return visible_sequences


class PSA_OT_export(Operator, ExportHelper):
    bl_idname = 'psa_export.operator'
    bl_label = 'Export'
    bl_options = {'INTERNAL', 'UNDO'}
    __doc__ = 'Export actions to PSA'
    filename_ext = '.psa'
    filter_glob: StringProperty(default='*.psa', options={'HIDDEN'})
    filepath: StringProperty(
        name='File Path',
        description='File path used for exporting the PSA file',
        maxlen=1024,
        default='')

    def __init__(self):
        self.armature_object = None

    @classmethod
    def poll(cls, context):
        try:
            cls._check_context(context)
        except RuntimeError as e:
            cls.poll_message_set(str(e))
            return False
        return True

    def draw(self, context):
        layout = self.layout
        pg = getattr(context.scene, 'psa_export')

        # FPS
        layout.prop(pg, 'fps_source', text='FPS')
        if pg.fps_source == 'CUSTOM':
            layout.prop(pg, 'fps_custom', text='Custom')

        # SOURCE
        layout.prop(pg, 'sequence_source', text='Source')

        if pg.sequence_source == 'TIMELINE_MARKERS':
            # ANIMDATA SOURCE
            layout.prop(pg, 'should_override_animation_data')
            if pg.should_override_animation_data:
                layout.prop(pg, 'animation_data_override', text='')

        # SELECT ALL/NONE
        row = layout.row(align=True)
        row.label(text='Select')
        row.operator(PSA_OT_export_actions_select_all.bl_idname, text='All', icon='CHECKBOX_HLT')
        row.operator(PSA_OT_export_actions_deselect_all.bl_idname, text='None', icon='CHECKBOX_DEHLT')

        # ACTIONS
        if pg.sequence_source == 'ACTIONS':
            rows = max(3, min(len(pg.action_list), 10))

            layout.template_list('PSA_UL_export_sequences', '', pg, 'action_list', pg, 'action_list_index', rows=rows)

            col = layout.column()
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(pg, 'sequence_name_prefix')
            col.prop(pg, 'sequence_name_suffix')

        elif pg.sequence_source == 'TIMELINE_MARKERS':
            rows = max(3, min(len(pg.marker_list), 10))
            layout.template_list('PSA_UL_export_sequences', '', pg, 'marker_list', pg, 'marker_list_index',
                                 rows=rows)

            col = layout.column()
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(pg, 'sequence_name_prefix')
            col.prop(pg, 'sequence_name_suffix')

        # Determine if there is going to be a naming conflict and display an error, if so.
        selected_items = [x for x in pg.action_list if x.is_selected]
        action_names = [x.name for x in selected_items]
        action_name_counts = Counter(action_names)
        for action_name, count in action_name_counts.items():
            if count > 1:
                layout.label(text=f'Duplicate action: {action_name}', icon='ERROR')
                break

        layout.separator()

        # BONES
        row = layout.row(align=True)
        row.prop(pg, 'bone_filter_mode', text='Bones')

        if pg.bone_filter_mode == 'BONE_GROUPS':
            row = layout.row(align=True)
            row.label(text='Select')
            row.operator(PSA_OT_export_bone_groups_select_all.bl_idname, text='All', icon='CHECKBOX_HLT')
            row.operator(PSA_OT_export_bone_groups_deselect_all.bl_idname, text='None', icon='CHECKBOX_DEHLT')
            rows = max(3, min(len(pg.bone_group_list), 10))
            layout.template_list('PSX_UL_bone_group_list', '', pg, 'bone_group_list', pg, 'bone_group_list_index',
                                 rows=rows)

        layout.prop(pg, 'should_enforce_bone_name_restrictions')

        layout.separator()

        # ROOT MOTION
        layout.prop(pg, 'root_motion', text='Root Motion')

    @classmethod
    def _check_context(cls, context):
        if context.view_layer.objects.active is None:
            raise RuntimeError('An armature must be selected')

        if context.view_layer.objects.active.type != 'ARMATURE':
            raise RuntimeError('The selected object must be an armature')

    def invoke(self, context, _event):
        try:
            self._check_context(context)
        except RuntimeError as e:
            self.report({'ERROR_INVALID_CONTEXT'}, str(e))

        pg: PSA_PG_export = getattr(context.scene, 'psa_export')

        self.armature_object = context.view_layer.objects.active

        if self.armature_object.animation_data is None:
            # This is required otherwise the action list will be empty if the armature has never had its animation
            # data created before (i.e. if no action was ever assigned to it).
            self.armature_object.animation_data_create()

        update_actions_and_timeline_markers(context, self.armature_object.data)

        # Populate bone groups list.
        populate_bone_group_list(self.armature_object, pg.bone_group_list)

        context.window_manager.fileselect_add(self)

        return {'RUNNING_MODAL'}

    def execute(self, context):
        pg = getattr(context.scene, 'psa_export')

        # Ensure that we actually have items that we are going to be exporting.
        if pg.sequence_source == 'ACTIONS' and len(pg.action_list) == 0:
            raise RuntimeError('No actions were selected for export')
        elif pg.sequence_source == 'TIMELINE_MARKERS' and len(pg.marker_list) == 0:
            raise RuntimeError('No timeline markers were selected for export')

        # Populate the export sequence list.
        animation_data_object = get_animation_data_object(context)
        animation_data = animation_data_object.animation_data

        if animation_data is None:
            raise RuntimeError(f'No animation data for object \'{animation_data_object.name}\'')

        export_sequences: List[PsaBuildSequence] = []

        if pg.sequence_source == 'ACTIONS':
            for action in filter(lambda x: x.is_selected, pg.action_list):
                if len(action.action.fcurves) == 0:
                    continue
                export_sequence = PsaBuildSequence()
                export_sequence.nla_state.action = action.action
                export_sequence.name = action.name
                export_sequence.nla_state.frame_start = action.frame_start
                export_sequence.nla_state.frame_end = action.frame_end
                export_sequence.fps = get_sequence_fps(context, pg.fps_source, pg.fps_custom, [action.action])
                export_sequence.compression_ratio = action.action.psa_export.compression_ratio
                export_sequence.key_quota = action.action.psa_export.key_quota
                export_sequences.append(export_sequence)
        elif pg.sequence_source == 'TIMELINE_MARKERS':
            for marker in pg.marker_list:
                export_sequence = PsaBuildSequence()
                export_sequence.name = marker.name
                export_sequence.nla_state.action = None
                export_sequence.nla_state.frame_start = marker.frame_start
                export_sequence.nla_state.frame_end = marker.frame_end
                nla_strips_actions = set(
                    map(lambda x: x.action, get_nla_strips_in_timeframe(animation_data, marker.frame_start, marker.frame_end)))
                export_sequence.fps = get_sequence_fps(context, pg.fps_source, pg.fps_custom, nla_strips_actions)
                export_sequences.append(export_sequence)
        else:
            raise ValueError(f'Unhandled sequence source: {pg.sequence_source}')

        options = PsaBuildOptions()
        options.animation_data = animation_data
        options.sequences = export_sequences
        options.bone_filter_mode = pg.bone_filter_mode
        options.bone_group_indices = [x.index for x in pg.bone_group_list if x.is_selected]
        options.should_ignore_bone_name_restrictions = pg.should_enforce_bone_name_restrictions
        options.sequence_name_prefix = pg.sequence_name_prefix
        options.sequence_name_suffix = pg.sequence_name_suffix
        options.root_motion = pg.root_motion

        try:
            psa = build_psa(context, options)
            self.report({'INFO'}, f'PSA export successful')
        except RuntimeError as e:
            self.report({'ERROR_INVALID_CONTEXT'}, str(e))
            return {'CANCELLED'}

        write_psa(psa, self.filepath)

        return {'FINISHED'}


class PSA_OT_export_actions_select_all(Operator):
    bl_idname = 'psa_export.sequences_select_all'
    bl_label = 'Select All'
    bl_description = 'Select all visible sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def get_item_list(cls, context):
        pg = context.scene.psa_export
        if pg.sequence_source == 'ACTIONS':
            return pg.action_list
        elif pg.sequence_source == 'TIMELINE_MARKERS':
            return pg.marker_list
        return None

    @classmethod
    def poll(cls, context):
        pg = getattr(context.scene, 'psa_export')
        item_list = cls.get_item_list(context)
        visible_sequences = get_visible_sequences(pg, item_list)
        has_unselected_sequences = any(map(lambda item: not item.is_selected, visible_sequences))
        return has_unselected_sequences

    def execute(self, context):
        pg = getattr(context.scene, 'psa_export')
        sequences = self.get_item_list(context)
        for sequence in get_visible_sequences(pg, sequences):
            sequence.is_selected = True
        return {'FINISHED'}


class PSA_OT_export_actions_deselect_all(Operator):
    bl_idname = 'psa_export.sequences_deselect_all'
    bl_label = 'Deselect All'
    bl_description = 'Deselect all visible sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def get_item_list(cls, context):
        pg = context.scene.psa_export
        if pg.sequence_source == 'ACTIONS':
            return pg.action_list
        elif pg.sequence_source == 'TIMELINE_MARKERS':
            return pg.marker_list
        return None

    @classmethod
    def poll(cls, context):
        item_list = cls.get_item_list(context)
        has_selected_items = any(map(lambda item: item.is_selected, item_list))
        return len(item_list) > 0 and has_selected_items

    def execute(self, context):
        pg = getattr(context.scene, 'psa_export')
        item_list = self.get_item_list(context)
        for sequence in get_visible_sequences(pg, item_list):
            sequence.is_selected = False
        return {'FINISHED'}


class PSA_OT_export_bone_groups_select_all(Operator):
    bl_idname = 'psa_export.bone_groups_select_all'
    bl_label = 'Select All'
    bl_description = 'Select all bone groups'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = getattr(context.scene, 'psa_export')
        item_list = pg.bone_group_list
        has_unselected_items = any(map(lambda action: not action.is_selected, item_list))
        return len(item_list) > 0 and has_unselected_items

    def execute(self, context):
        pg = getattr(context.scene, 'psa_export')
        for item in pg.bone_group_list:
            item.is_selected = True
        return {'FINISHED'}


class PSA_OT_export_bone_groups_deselect_all(Operator):
    bl_idname = 'psa_export.bone_groups_deselect_all'
    bl_label = 'Deselect All'
    bl_description = 'Deselect all bone groups'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = getattr(context.scene, 'psa_export')
        item_list = pg.bone_group_list
        has_selected_actions = any(map(lambda action: action.is_selected, item_list))
        return len(item_list) > 0 and has_selected_actions

    def execute(self, context):
        pg = getattr(context.scene, 'psa_export')
        for action in pg.bone_group_list:
            action.is_selected = False
        return {'FINISHED'}


classes = (
    PSA_OT_export,
    PSA_OT_export_actions_select_all,
    PSA_OT_export_actions_deselect_all,
    PSA_OT_export_bone_groups_select_all,
    PSA_OT_export_bone_groups_deselect_all,
)
