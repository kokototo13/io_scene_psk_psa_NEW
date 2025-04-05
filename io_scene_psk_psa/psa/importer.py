import typing
from typing import List, Optional

import bpy
import numpy
from bpy.types import FCurve, Object, Context
from mathutils import Vector, Quaternion

from .data import Psa
from .reader import PsaReader


class PsaImportOptions(object):
    def __init__(self):
        self.should_use_fake_user = False
        self.should_stash = False
        self.sequence_names = []
        self.should_overwrite = False
        self.should_write_keyframes = True
        self.should_write_metadata = True
        self.action_name_prefix = ''
        self.should_convert_to_samples = False
        self.bone_mapping_mode = 'CASE_INSENSITIVE'


class ImportBone(object):
    def __init__(self, psa_bone: Psa.Bone):
        self.psa_bone: Psa.Bone = psa_bone
        self.parent: Optional[ImportBone] = None
        self.armature_bone = None
        self.pose_bone = None
        self.orig_loc: Vector = Vector()
        self.orig_quat: Quaternion = Quaternion()
        self.post_quat: Quaternion = Quaternion()
        self.fcurves: List[FCurve] = []


def _calculate_fcurve_data(import_bone: ImportBone, key_data: typing.Iterable[float]):
    # Convert world-space transforms to local-space transforms.
    key_rotation = Quaternion(key_data[0:4])
    key_location = Vector(key_data[4:])
    q = import_bone.post_quat.copy()
    q.rotate(import_bone.orig_quat)
    quat = q
    q = import_bone.post_quat.copy()
    if import_bone.parent is None:
        q.rotate(key_rotation.conjugated())
    else:
        q.rotate(key_rotation)
    quat.rotate(q.conjugated())
    loc = key_location - import_bone.orig_loc
    loc.rotate(import_bone.post_quat.conjugated())
    return quat.w, quat.x, quat.y, quat.z, loc.x, loc.y, loc.z


class PsaImportResult:
    def __init__(self):
        self.warnings: List[str] = []


def _get_armature_bone_index_for_psa_bone(psa_bone_name: str, armature_bone_names: List[str], bone_mapping_mode: str = 'EXACT') -> Optional[int]:
    """
    @param psa_bone_name: The name of the PSA bone.
    @param armature_bone_names: The names of the bones in the armature.
    @param bone_mapping_mode: One of 'EXACT' or 'CASE_INSENSITIVE'.
    @return: The index of the armature bone that corresponds to the given PSA bone, or None if no such bone exists.
    """
    for armature_bone_index, armature_bone_name in enumerate(armature_bone_names):
        if bone_mapping_mode == 'CASE_INSENSITIVE':
            if armature_bone_name.lower() == psa_bone_name.lower():
                return armature_bone_index
        else:
            if armature_bone_name == psa_bone_name:
                return armature_bone_index
    return None


def import_psa(context: Context, psa_reader: PsaReader, armature_object: Object, options: PsaImportOptions) -> PsaImportResult:
    result = PsaImportResult()
    sequences = [psa_reader.sequences[x] for x in options.sequence_names]
    armature_data = typing.cast(bpy.types.Armature, armature_object.data)

    # Create an index mapping from bones in the PSA to bones in the target armature.
    psa_to_armature_bone_indices = {}
    armature_to_psa_bone_indices = {}
    armature_bone_names = [x.name for x in armature_data.bones]
    psa_bone_names = []
    duplicate_mappings = []

    for psa_bone_index, psa_bone in enumerate(psa_reader.bones):
        psa_bone_name: str = psa_bone.name.decode('windows-1252')
        armature_bone_index = _get_armature_bone_index_for_psa_bone(psa_bone_name, armature_bone_names, options.bone_mapping_mode)
        if armature_bone_index is not None:
            # Ensure that no other PSA bone has been mapped to this armature bone yet.
            if armature_bone_index not in armature_to_psa_bone_indices:
                psa_to_armature_bone_indices[psa_bone_index] = armature_bone_names.index(psa_bone_name)
                armature_to_psa_bone_indices[armature_bone_index] = psa_bone_index
            else:
                # This armature bone has already been mapped to a PSA bone.
                duplicate_mappings.append((psa_bone_index, armature_bone_index, armature_to_psa_bone_indices[armature_bone_index]))
            psa_bone_names.append(armature_bone_names[armature_bone_index])
        else:
            psa_bone_names.append(psa_bone_name)

    # Warn about duplicate bone mappings.
    if len(duplicate_mappings) > 0:
        for (psa_bone_index, armature_bone_index, mapped_psa_bone_index) in duplicate_mappings:
            psa_bone_name = psa_bone_names[psa_bone_index]
            armature_bone_name = armature_bone_names[armature_bone_index]
            mapped_psa_bone_name = psa_bone_names[mapped_psa_bone_index]
            result.warnings.append(f'PSA bone {psa_bone_index} ({psa_bone_name}) could not be mapped to armature bone {armature_bone_index} ({armature_bone_name}) because the armature bone is already mapped to PSA bone {mapped_psa_bone_index} ({mapped_psa_bone_name})')

    # Report if there are missing bones in the target armature.
    missing_bone_names = set(psa_bone_names).difference(set(armature_bone_names))
    if len(missing_bone_names) > 0:
        result.warnings.append(
            f'The armature \'{armature_object.name}\' is missing {len(missing_bone_names)} bones that exist in '
            'the PSA:\n' +
            str(list(sorted(missing_bone_names)))
        )
    del armature_bone_names

    # Create intermediate bone data for import operations.
    import_bones = []
    import_bones_dict = dict()

    for (psa_bone_index, psa_bone), psa_bone_name in zip(enumerate(psa_reader.bones), psa_bone_names):
        if psa_bone_index not in psa_to_armature_bone_indices:
            # PSA bone does not map to armature bone, skip it and leave an empty bone in its place.
            import_bones.append(None)
            continue
        import_bone = ImportBone(psa_bone)
        import_bone.armature_bone = armature_data.bones[psa_bone_name]
        import_bone.pose_bone = armature_object.pose.bones[psa_bone_name]
        import_bones_dict[psa_bone_name] = import_bone
        import_bones.append(import_bone)

    for import_bone in filter(lambda x: x is not None, import_bones):
        armature_bone = import_bone.armature_bone
        if armature_bone.parent is not None and armature_bone.parent.name in psa_bone_names:
            import_bone.parent = import_bones_dict[armature_bone.parent.name]
        # Calculate the original location & rotation of each bone (in world-space maybe?)
        if armature_bone.get('orig_quat') is not None:
            # TODO: ideally we don't rely on bone auxiliary data like this, the non-aux data path is incorrect
            # (animations are flipped 180 around Z)
            import_bone.orig_quat = Quaternion(armature_bone['orig_quat'])
            import_bone.orig_loc = Vector(armature_bone['orig_loc'])
            import_bone.post_quat = Quaternion(armature_bone['post_quat'])
        else:
            if import_bone.parent is not None:
                import_bone.orig_loc = armature_bone.matrix_local.translation - armature_bone.parent.matrix_local.translation
                import_bone.orig_loc.rotate(armature_bone.parent.matrix_local.to_quaternion().conjugated())
                import_bone.orig_quat = armature_bone.matrix_local.to_quaternion()
                import_bone.orig_quat.rotate(armature_bone.parent.matrix_local.to_quaternion().conjugated())
                import_bone.orig_quat.conjugate()
            else:
                import_bone.orig_loc = armature_bone.matrix_local.translation.copy()
                import_bone.orig_quat = armature_bone.matrix_local.to_quaternion()
            import_bone.post_quat = import_bone.orig_quat.conjugated()

    context.window_manager.progress_begin(0, len(sequences))

    # Create and populate the data for new sequences.
    actions = []
    for sequence_index, sequence in enumerate(sequences):
        # Add the action.
        sequence_name = sequence.name.decode('windows-1252')
        action_name = options.action_name_prefix + sequence_name

        if options.should_overwrite and action_name in bpy.data.actions:
            action = bpy.data.actions[action_name]
        else:
            action = bpy.data.actions.new(name=action_name)

        if options.should_write_keyframes:
            # Remove existing f-curves (replace with action.fcurves.clear() in Blender 3.2)
            while len(action.fcurves) > 0:
                action.fcurves.remove(action.fcurves[-1])

            # Create f-curves for the rotation and location of each bone.
            for psa_bone_index, armature_bone_index in psa_to_armature_bone_indices.items():
                import_bone = import_bones[psa_bone_index]
                pose_bone = import_bone.pose_bone
                rotation_data_path = pose_bone.path_from_id('rotation_quaternion')
                location_data_path = pose_bone.path_from_id('location')
                import_bone.fcurves = [
                    action.fcurves.new(rotation_data_path, index=0, action_group=pose_bone.name),  # Qw
                    action.fcurves.new(rotation_data_path, index=1, action_group=pose_bone.name),  # Qx
                    action.fcurves.new(rotation_data_path, index=2, action_group=pose_bone.name),  # Qy
                    action.fcurves.new(rotation_data_path, index=3, action_group=pose_bone.name),  # Qz
                    action.fcurves.new(location_data_path, index=0, action_group=pose_bone.name),  # Lx
                    action.fcurves.new(location_data_path, index=1, action_group=pose_bone.name),  # Ly
                    action.fcurves.new(location_data_path, index=2, action_group=pose_bone.name),  # Lz
                ]

            # Read the sequence data matrix from the PSA.
            sequence_data_matrix = psa_reader.read_sequence_data_matrix(sequence_name)

            # Convert the sequence's data from world-space to local-space.
            for bone_index, import_bone in enumerate(import_bones):
                if import_bone is None:
                    continue
                for frame_index in range(sequence.frame_count):
                    # This bone has writeable keyframes for this frame.
                    key_data = sequence_data_matrix[frame_index, bone_index]
                    # Calculate the local-space key data for the bone.
                    sequence_data_matrix[frame_index, bone_index] = _calculate_fcurve_data(import_bone, key_data)

            # Write the keyframes out.
            fcurve_data = numpy.zeros(2 * sequence.frame_count, dtype=float)
            fcurve_data[0::2] = range(sequence.frame_count)
            for bone_index, import_bone in enumerate(import_bones):
                if import_bone is None:
                    continue
                for fcurve_index, fcurve in enumerate(import_bone.fcurves):
                    fcurve_data[1::2] = sequence_data_matrix[:, bone_index, fcurve_index]
                    fcurve.keyframe_points.add(sequence.frame_count)
                    fcurve.keyframe_points.foreach_set('co', fcurve_data)

            if options.should_convert_to_samples:
                # Bake the curve to samples.
                for fcurve in action.fcurves:
                    fcurve.convert_to_samples(start=0, end=sequence.frame_count)

        # Write meta-data.
        if options.should_write_metadata:
            action['psa_sequence_fps'] = sequence.fps

        action.use_fake_user = options.should_use_fake_user

        actions.append(action)

        context.window_manager.progress_update(sequence_index)

    # If the user specifies, store the new animations as strips on a non-contributing NLA track.
    if options.should_stash:
        if armature_object.animation_data is None:
            armature_object.animation_data_create()
        for action in actions:
            nla_track = armature_object.animation_data.nla_tracks.new()
            nla_track.name = action.name
            nla_track.mute = True
            nla_track.strips.new(name=action.name, start=0, action=action)

    context.window_manager.progress_end()

    return result
