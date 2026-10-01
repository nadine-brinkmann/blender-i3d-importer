"""Restore merged FS25 tree subsets as editable branches with real pivots.

GIANTS rebuilds the combined type-5 shape when loading the exported children
of a splitType trunk. Geometry is already in trunk space; recover branch-local positions using
the recorded translation, rotation and scale before the importer axis bake.
"""
import bpy
from mathutils import Vector, Matrix, Euler
import math


def restore_tree_attachments(collection, shape_map, shape_id_to_obj, report):
    bpy.context.view_layer.update()
    sources = [o for o in collection.all_objects if '_i3d_tree_shape_id' in o]
    for source in sources:
        shape_id = int(source['_i3d_tree_shape_id'])
        shape = shape_map[shape_id]
        records = getattr(shape, 'tree_attachments', [])
        if not records:
            continue
        if source.parent is None or not source.parent.get('i3D_splitType', 0):
            report('WARNING', f'{source.name}: attachment parent has no splitType; kept unchanged')
            continue
        if source.children or len(source.data.materials) != 1:
            report('WARNING', f'{source.name}: unsupported attachment hierarchy/material layout; kept unchanged')
            continue
        if len(records) != len(shape.subsets):
            raise ValueError('Tree attachment/subset count mismatch')

        if any(not all(math.isfinite(v) for v in rec['translation'] + rec['rotation'] + rec['scale'])
               or any(abs(v) < 1e-10 for v in rec['scale']) for rec in records):
            raise ValueError('Invalid tree attachment transform')

        # Validate every range before replacing the original combined object.
        for subset in shape.subsets:
            lo, hi = subset.first_vertex, subset.first_vertex + subset.num_vertices
            if not (0 <= lo <= hi <= len(shape.positions)) or subset.first_index % 3 or subset.num_indices % 3:
                raise ValueError('Invalid tree attachment subset range')
            tris = shape.triangles[subset.first_index // 3:(subset.first_index + subset.num_indices) // 3]
            if len(tris) != subset.num_indices // 3 or any(
                    not lo <= idx - 1 < hi for t in tris for idx in (t.p1, t.p2, t.p3)):
                raise ValueError('Tree attachment triangles cross subset boundaries')

        original_world = source.matrix_world.copy()
        for slot, (subset, record) in enumerate(zip(shape.subsets, records)):
            lo, hi = subset.first_vertex, subset.first_vertex + subset.num_vertices
            pivot = Vector(record['translation'])
            name = f'{source.name}_branch{slot:03d}'
            mesh = bpy.data.meshes.new(name)
            rotation = Euler(record['rotation'], 'XYZ').to_matrix().to_4x4()
            scale = Matrix.Diagonal((*record['scale'], 1.0))
            local_transform = Matrix.Translation(pivot) @ rotation @ scale
            inverse = local_transform.inverted()
            positions = [inverse @ Vector((p.x, p.y, p.z)) for p in shape.positions[lo:hi]]
            tris = shape.triangles[subset.first_index // 3:(subset.first_index + subset.num_indices) // 3]
            faces = [(t.p1 - 1 - lo, t.p2 - 1 - lo, t.p3 - 1 - lo) for t in tris]
            mesh.from_pydata(positions, [], faces)
            mesh.materials.append(source.data.materials[0])
            for channel, values in enumerate(shape.uv_sets):
                if values is not None:
                    layer = mesh.uv_layers.new(name='UVMap' if channel == 0 else f'UV{channel+1}')
                    layer.data.foreach_set('uv', [v for loop in mesh.loops
                        for v in (values[lo + loop.vertex_index].u, values[lo + loop.vertex_index].v)])
            if shape.vertex_colors is not None:
                layer = mesh.color_attributes.new(name='Color', type='FLOAT_COLOR', domain='CORNER')
                layer.data.foreach_set('color', [v for loop in mesh.loops
                    for c in [shape.vertex_colors[lo + loop.vertex_index]] for v in (c.x, c.y, c.z, c.w)])
            # Preserve this stream for exporters that support generic attributes.
            if shape.generic_data is not None:
                layer = mesh.attributes.new(name='generic', type='FLOAT', domain='POINT')
                layer.data.foreach_set('value', shape.generic_data[lo:hi])
            for poly in mesh.polygons:
                poly.use_smooth = True
            if shape.normals is not None:
                normal_to_local = local_transform.to_3x3().transposed()
                mesh.normals_split_custom_set_from_vertices([normal_to_local @ Vector((n.x, n.y, n.z)) for n in shape.normals[lo:hi]])
            obj = bpy.data.objects.new(name, mesh)
            collection.objects.link(obj)
            obj.parent = source.parent
            axis = Matrix.Rotation(math.pi / 2, 4, 'X')
            obj.matrix_world = original_world @ Matrix.Translation(pivot) @ (axis @ rotation @ axis.inverted()) @ scale
            for key, value in source.items():
                if key.startswith('i3D_') and key not in ('i3D_mergeChildren', 'i3D_mergeGroup', 'i3D_mergeGroupRoot'):
                    obj[key] = value
            obj['_i3d_kind'] = 'Shape'
            obj['_i3d_tree_attachment_slot'] = slot
            obj['_i3d_tree_face_index'] = record['face_index']
        source_name = source.name
        old_mesh = source.data
        bpy.data.objects.remove(source, do_unlink=True)
        if old_mesh.users == 0:
            bpy.data.meshes.remove(old_mesh)
        shape_id_to_obj.pop(shape_id, None)
        report('INFO', f'{source_name}: restored {len(records)} tree attachments with original pivots')
