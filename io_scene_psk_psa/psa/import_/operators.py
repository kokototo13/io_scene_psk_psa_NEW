import os

from bpy.props import StringProperty
from bpy.types import Operator, Event, Context
from bpy_extras.io_utils import ImportHelper

from .properties import get_visible_sequences
from ..importer import import_psa, PsaImportOptions
from ..reader import PsaReader


class PSA_OT_import_sequences_from_text(Operator):
    bl_idname = 'psa_import.sequences_select_from_text'
    bl_label = 'Select By Text List'
    bl_description = 'Select sequences by name from text list'
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        pg = getattr(context.scene, 'psa_import')
        return len(pg.sequence_list) > 0

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=256)

    def draw(self, context):
        layout = self.layout
        pg = getattr(context.scene, 'psa_import')
        layout.label(icon='INFO', text='Each sequence name should be on a new line.')
        layout.prop(pg, 'select_text', text='')

    def execute(self, context):
        pg = getattr(context.scene, 'psa_import')
        if pg.select_text is None:
            self.report({'ERROR_INVALID_CONTEXT'}, 'No text block selected')
            return {'CANCELLED'}
        contents = pg.select_text.as_string()
        count = 0
        for line in contents.split('\n'):
            for sequence in pg.sequence_list:
                if sequence.action_name == line:
                    sequence.is_selected = True
                    count += 1
        self.report({'INFO'}, f'Selected {count} sequence(s)')
        return {'FINISHED'}


class PSA_OT_import_sequences_select_all(Operator):
    bl_idname = 'psa_import.sequences_select_all'
    bl_label = 'All'
    bl_description = 'Select all sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = getattr(context.scene, 'psa_import')
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        has_unselected_actions = any(map(lambda action: not action.is_selected, visible_sequences))
        return len(visible_sequences) > 0 and has_unselected_actions

    def execute(self, context):
        pg = getattr(context.scene, 'psa_import')
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        for sequence in visible_sequences:
            sequence.is_selected = True
        return {'FINISHED'}


class PSA_OT_import_sequences_deselect_all(Operator):
    bl_idname = 'psa_import.sequences_deselect_all'
    bl_label = 'None'
    bl_description = 'Deselect all visible sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = getattr(context.scene, 'psa_import')
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        has_selected_sequences = any(map(lambda sequence: sequence.is_selected, visible_sequences))
        return len(visible_sequences) > 0 and has_selected_sequences

    def execute(self, context):
        pg = getattr(context.scene, 'psa_import')
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        for sequence in visible_sequences:
            sequence.is_selected = False
        return {'FINISHED'}


class PSA_OT_import_select_file(Operator):
    bl_idname = 'psa_import.select_file'
    bl_label = 'Select'
    bl_options = {'INTERNAL'}
    bl_description = 'Select a PSA file from which to import animations'
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default="*.psa", options={'HIDDEN'})

    def execute(self, context):
        getattr(context.scene, 'psa_import').psa_file_path = self.filepath
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


def load_psa_file(context, filepath: str):
    pg = context.scene.psa_import
    pg.sequence_list.clear()
    pg.psa.bones.clear()
    pg.psa_error = ''
    try:
        # Read the file and populate the action list.
        p = os.path.abspath(filepath)
        psa_reader = PsaReader(p)
        for sequence in psa_reader.sequences.values():
            item = pg.sequence_list.add()
            item.action_name = sequence.name.decode('windows-1252')
        for psa_bone in psa_reader.bones:
            item = pg.psa.bones.add()
            item.bone_name = psa_bone.name.decode('windows-1252')
    except Exception as e:
        pg.psa_error = str(e)



def on_psa_file_path_updated(cls, context):
    load_psa_file(context, cls.filepath)


class PSA_OT_import(Operator, ImportHelper):
    bl_idname = 'psa_import.import'
    bl_label = 'Import'
    bl_description = 'Import the selected animations into the scene as actions'
    bl_options = {'INTERNAL', 'UNDO'}

    filename_ext = '.psa'
    filter_glob: StringProperty(default='*.psa', options={'HIDDEN'})
    filepath: StringProperty(
        name='File Path',
        description='File path used for importing the PSA file',
        maxlen=1024,
        default='',
        update=on_psa_file_path_updated)

    @classmethod
    def poll(cls, context):
        active_object = context.view_layer.objects.active
        if active_object is None or active_object.type != 'ARMATURE':
            cls.poll_message_set('The active object must be an armature')
            return False
        return True

    def execute(self, context):
        pg = getattr(context.scene, 'psa_import')
        psa_reader = PsaReader(self.filepath)
        sequence_names = [x.action_name for x in pg.sequence_list if x.is_selected]

        options = PsaImportOptions()
        options.sequence_names = sequence_names
        options.should_use_fake_user = pg.should_use_fake_user
        options.should_stash = pg.should_stash
        options.action_name_prefix = pg.action_name_prefix if pg.should_use_action_name_prefix else ''
        options.should_overwrite = pg.should_overwrite
        options.should_write_metadata = pg.should_write_metadata
        options.should_write_keyframes = pg.should_write_keyframes
        options.should_convert_to_samples = pg.should_convert_to_samples
        options.bone_mapping_mode = pg.bone_mapping_mode

        if len(sequence_names) == 0:
            self.report({'ERROR_INVALID_CONTEXT'}, 'No sequences selected')
            return {'CANCELLED'}

        result = import_psa(context, psa_reader, context.view_layer.objects.active, options)

        if len(result.warnings) > 0:
            message = f'Imported {len(sequence_names)} action(s) with {len(result.warnings)} warning(s)\n'
            self.report({'WARNING'}, message)
            for warning in result.warnings:
                self.report({'WARNING'}, warning)
        else:
            self.report({'INFO'}, f'Imported {len(sequence_names)} action(s)')

        return {'FINISHED'}

    def invoke(self, context: Context, event: Event):
        # Attempt to load the PSA file for the pre-selected file.
        load_psa_file(context, self.filepath)

        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def draw(self, context: Context):
        layout = self.layout
        pg = getattr(context.scene, 'psa_import')

        if pg.psa_error:
            row = layout.row()
            row.label(text='Select a PSA file', icon='ERROR')
        else:
            box = layout.box()

            box.label(text=f'Sequences ({len(pg.sequence_list)})', icon='ARMATURE_DATA')

            # Select buttons.
            rows = max(3, min(len(pg.sequence_list), 10))

            row = box.row()
            col = row.column()

            row2 = col.row(align=True)
            row2.label(text='Select')
            row2.operator(PSA_OT_import_sequences_from_text.bl_idname, text='', icon='TEXT')
            row2.operator(PSA_OT_import_sequences_select_all.bl_idname, text='All', icon='CHECKBOX_HLT')
            row2.operator(PSA_OT_import_sequences_deselect_all.bl_idname, text='None', icon='CHECKBOX_DEHLT')

            col = col.row()
            col.template_list('PSA_UL_import_sequences', '', pg, 'sequence_list', pg, 'sequence_list_index', rows=rows)

        col = layout.column(heading='')
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(pg, 'should_overwrite')

        col = layout.column(heading='Write')
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(pg, 'should_write_keyframes')
        col.prop(pg, 'should_write_metadata')

        col = layout.column()
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(pg, 'bone_mapping_mode')

        if pg.should_write_keyframes:
            col = layout.column(heading='Keyframes')
            col.use_property_split = True
            col.use_property_decorate = False
            col.prop(pg, 'should_convert_to_samples')
            col.separator()

        col = layout.column(heading='Options')
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(pg, 'should_use_fake_user')
        col.prop(pg, 'should_stash')
        col.prop(pg, 'should_use_action_name_prefix')

        if pg.should_use_action_name_prefix:
            col.prop(pg, 'action_name_prefix')


classes = (
    PSA_OT_import_sequences_select_all,
    PSA_OT_import_sequences_deselect_all,
    PSA_OT_import_sequences_from_text,
    PSA_OT_import,
    PSA_OT_import_select_file,
)
