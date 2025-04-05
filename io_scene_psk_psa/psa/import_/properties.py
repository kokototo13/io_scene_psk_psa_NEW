import re
from fnmatch import fnmatch
from typing import List

from bpy.props import StringProperty, BoolProperty, CollectionProperty, IntProperty, PointerProperty, EnumProperty
from bpy.types import PropertyGroup, Text

empty_set = set()


class PSA_PG_import_action_list_item(PropertyGroup):
    action_name: StringProperty(options=empty_set)
    is_selected: BoolProperty(default=True, options=empty_set)


class PSA_PG_bone(PropertyGroup):
    bone_name: StringProperty(options=empty_set)


class PSA_PG_data(PropertyGroup):
    bones: CollectionProperty(type=PSA_PG_bone)
    sequence_count: IntProperty(default=0)


class PSA_PG_import(PropertyGroup):
    psa_error: StringProperty(default='')
    psa: PointerProperty(type=PSA_PG_data)
    sequence_list: CollectionProperty(type=PSA_PG_import_action_list_item)
    sequence_list_index: IntProperty(name='', default=0)
    should_use_fake_user: BoolProperty(default=True, name='Fake User',
                                       description='Assign each imported action a fake user so that the data block is '
                                                   'saved even it has no users',
                                       options=empty_set)
    should_stash: BoolProperty(default=False, name='Stash',
                               description='Stash each imported action as a strip on a new non-contributing NLA track',
                               options=empty_set)
    should_use_action_name_prefix: BoolProperty(default=False, name='Prefix Action Name', options=empty_set)
    action_name_prefix: StringProperty(default='', name='Prefix', options=empty_set)
    should_overwrite: BoolProperty(default=False, name='Overwrite', options=empty_set,
                                   description='If an action with a matching name already exists, the existing action '
                                               'will have it\'s data overwritten instead of a new action being created')
    should_write_keyframes: BoolProperty(default=True, name='Keyframes', options=empty_set)
    should_write_metadata: BoolProperty(default=True, name='Metadata', options=empty_set,
                                        description='Additional data will be written to the custom properties of the '
                                                    'Action (e.g., frame rate)')
    sequence_filter_name: StringProperty(default='', options={'TEXTEDIT_UPDATE'})
    sequence_filter_is_selected: BoolProperty(default=False, options=empty_set, name='Only Show Selected',
                                              description='Only show selected sequences')
    sequence_use_filter_invert: BoolProperty(default=False, options=empty_set)
    sequence_use_filter_regex: BoolProperty(default=False, name='Regular Expression',
                                            description='Filter using regular expressions', options=empty_set)
    select_text: PointerProperty(type=Text)
    should_convert_to_samples: BoolProperty(
        default=False,
        name='Convert to Samples',
        description='Convert keyframes to read-only samples. '
                    'Recommended if you do not plan on editing the actions directly'
    )
    bone_mapping_mode: EnumProperty(
        name='Bone Mapping',
        options=empty_set,
        description='The method by which bones from the incoming PSA file are mapped to the armature',
        items=(
            ('EXACT', 'Exact', 'Bone names must match exactly.', 'EXACT', 0),
            ('CASE_INSENSITIVE', 'Case Insensitive', 'Bones names must match, ignoring case (e.g., the bone PSA bone '
             '\'root\' can be mapped to the armature bone \'Root\')', 'CASE_INSENSITIVE', 1),
        )
    )


def filter_sequences(pg: PSA_PG_import, sequences) -> List[int]:
    bitflag_filter_item = 1 << 30
    flt_flags = [bitflag_filter_item] * len(sequences)

    if pg.sequence_filter_name is not None:
        # Filter name is non-empty.
        if pg.sequence_use_filter_regex:
            # Use regular expression. If regex pattern doesn't compile, just ignore it.
            try:
                regex = re.compile(pg.sequence_filter_name)
                for i, sequence in enumerate(sequences):
                    if not regex.match(sequence.action_name):
                        flt_flags[i] &= ~bitflag_filter_item
            except re.error:
                pass
        else:
            # User regular text matching.
            for i, sequence in enumerate(sequences):
                if not fnmatch(sequence.action_name, f'*{pg.sequence_filter_name}*'):
                    flt_flags[i] &= ~bitflag_filter_item

    if pg.sequence_filter_is_selected:
        for i, sequence in enumerate(sequences):
            if not sequence.is_selected:
                flt_flags[i] &= ~bitflag_filter_item

    if pg.sequence_use_filter_invert:
        # Invert filter flags for all items.
        for i, sequence in enumerate(sequences):
            flt_flags[i] ^= bitflag_filter_item

    return flt_flags


def get_visible_sequences(pg: PSA_PG_import, sequences) -> List[PSA_PG_import_action_list_item]:
    bitflag_filter_item = 1 << 30
    visible_sequences = []
    for i, flag in enumerate(filter_sequences(pg, sequences)):
        if bool(flag & bitflag_filter_item):
            visible_sequences.append(sequences[i])
    return visible_sequences


classes = (
    PSA_PG_import_action_list_item,
    PSA_PG_bone,
    PSA_PG_data,
    PSA_PG_import,
)
