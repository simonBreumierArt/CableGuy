import bpy
from math import sin, cos, pi
from mathutils import Vector
from mathutils.bvhtree import BVHTree

bl_info = {
    "name": "CableGuy",
    "author": "Simon Breumier",
    "version": (1, 21, 5),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Câble",
    "description": "Génère un câble interactif avec conservation des formes personnalisées, bouts sortants lissés contrôlables et gestion des collisions",
    "category": "Add Curve",
}


def get_or_create_anchors_collection(context):
    """Récupère ou crée la collection 'Ancres' avec une étiquette de couleur verte."""
    coll_name = "Ancres"
    coll = bpy.data.collections.get(coll_name)
    
    if coll is None:
        coll = bpy.data.collections.new(coll_name)
        context.scene.collection.children.link(coll)
        coll.color_tag = 'COLOR_04'  # Vert dans l'Outliner
    elif coll.name not in context.scene.collection.children:
        try:
            context.scene.collection.children.link(coll)
        except RuntimeError:
            pass

    return coll


def setup_curve_to_tube_modifier(obj, radius, ring):
    """Crée ou met à jour le modificateur natif 'Curve to Tube' (Blender 5.0+)"""
    mod_name = "Curve to Tube"
    mod = obj.modifiers.get(mod_name)

    if not mod:
        view_layer = bpy.context.view_layer
        active_obj = view_layer.objects.active
        view_layer.objects.active = obj
        try:
            bpy.ops.object.modifier_add_node_group(
                asset_library_type='ESSENTIALS', 
                asset_library_identifier="", 
                relative_asset_identifier="nodes/geometry_nodes_essentials.blend/NodeTree/Curve to Tube"
            )
        except Exception:
            pass
        finally:
            view_layer.objects.active = active_obj

    mod = obj.modifiers.get(mod_name)
    blender_version = bpy.app.version
    if mod:
        try:
            if blender_version >= (5, 2, 0):
                            bpy.context.object.modifiers["Curve to Tube"].properties.inputs.Socket_5.value = radius
                            bpy.context.object.modifiers["Curve to Tube"].properties.inputs.Socket_2.value = ring
            elif blender_version >= (5, 0, 0):
                mod["Socket_5"] = radius
                mod["Socket_2"] = ring    
        except KeyError:
            pass
        
    return mod


def get_collider_trees(collection, depsgraph, current_obj=None):
    trees = []
    if not collection:
        return trees

    valid_mesh_types = {'MESH', 'CURVE', 'SURFACE', 'FONT', 'META'}

    for obj in collection.all_objects:
        if obj == current_obj or obj.type not in valid_mesh_types:
            continue
            
        if obj.visible_get():
            try:
                eval_obj = obj.evaluated_get(depsgraph)
                mesh = eval_obj.to_mesh()
                if mesh:
                    matrix = obj.matrix_world
                    verts = [matrix @ v.co for v in mesh.vertices]
                    polys = [p.vertices for p in mesh.polygons]
                    if verts and polys:
                        bvh = BVHTree.FromPolygons(verts, polys)
                        trees.append(bvh)
                    eval_obj.to_mesh_clear()
            except RuntimeError:
                continue
    return trees


def get_or_create_unique_empty(base_name, location, context):
    """Crée ou récupère un Empty de manière unique et le place dans la collection 'Ancres'."""
    name = base_name
    counter = 1
    emp = None
    
    while name in bpy.data.objects:
        existing_obj = bpy.data.objects[name]
        is_used = False
        for c in bpy.data.objects:
            if c.type == 'CURVE':
                p1 = getattr(c, "cable_p1", None)
                p2 = getattr(c, "cable_p2", None)
                if (p1 and p1.name == existing_obj.name) or (p2 and p2.name == existing_obj.name):
                    is_used = True
                    break
        if not is_used:
            emp = existing_obj
            break
        name = f"{base_name}_{counter}"
        counter += 1
        
    anchors_coll = get_or_create_anchors_collection(context)

    if emp is None:
        emp = bpy.data.objects.new(name, None)
        emp.empty_display_type = 'SPHERE'
        emp.empty_display_size = 0.2
        anchors_coll.objects.link(emp)
    else:
        if emp.name not in anchors_coll.objects:
            anchors_coll.objects.link(emp)
            target_coll = context.collection if context.collection else context.scene.collection
            if emp.name in target_coll.objects and target_coll != anchors_coll:
                target_coll.objects.unlink(emp)
            
    emp.location = location
    emp.hide_set(False)
    emp.hide_viewport = False
    return emp


def get_resampled_points(pts, res):
    """Rééchantillonne une liste de points de manière uniforme selon leur longueur d'arc."""
    if len(pts) < 2:
        return pts
    cum_lens = [0.0]
    for i in range(1, len(pts)):
        cum_lens.append(cum_lens[-1] + (pts[i] - pts[i-1]).length)
    total_len = cum_lens[-1]
    if total_len < 1e-6:
        return [pts[0]] * res
        
    res_pts = []
    for i in range(res):
        t_target = i / (res - 1) * total_len
        idx = 0
        while idx < len(cum_lens) - 1 and cum_lens[idx + 1] < t_target:
            idx += 1
        if idx >= len(cum_lens) - 1:
            res_pts.append(pts[-1].copy())
        else:
            seg_len = cum_lens[idx + 1] - cum_lens[idx]
            frac = (t_target - cum_lens[idx]) / seg_len if seg_len > 1e-6 else 0.0
            res_pts.append(pts[idx].lerp(pts[idx+1], frac))
    return res_pts


def update_cable_geometry(obj, context=None):
    if not obj or obj.type != 'CURVE':
        return

    if obj.mode == 'EDIT':
        return

    curve_data = obj.data
    if not obj.cable_p1 or not obj.cable_p2:
        return

    bevel = max(0.001, obj.cable_bevel)
    setup_curve_to_tube_modifier(obj, bevel, obj.rings)

    p1_w = obj.cable_p1.matrix_world.translation.copy()
    p2_w = obj.cable_p2.matrix_world.translation.copy()
    res = max(4, obj.cable_res)

    is_twined = getattr(obj, "cable_is_twined", False)
    source_obj = getattr(obj, "cable_source", None)

    # CAS DU CÂBLE ENTORTILLÉ (Avec transport parallèle stable et fondu de fusion aux ancres)
    if is_twined and source_obj and source_obj != obj:
        source_spline = source_obj.data.splines[0] if source_obj.data.splines else None
        if source_spline:
            matrix = source_obj.matrix_world
            source_pts = [matrix @ Vector(p.co[:3]) for p in source_spline.points]

            if len(source_pts) < 2:
                source_pts = [p1_w, p2_w]

            uniform_source_pts = get_resampled_points(source_pts, res)
            turns = getattr(obj, "cable_turns", 8.0)
            offset_val = getattr(obj, "cable_offset", 0.0)
            helix_radius = source_obj.cable_bevel + obj.cable_bevel + 0.005

            # 1. Lissage préalable des points sources pour éliminer les micro-cassures
            smoothed_source = list(uniform_source_pts)
            for _ in range(2):
                temp_pts = list(smoothed_source)
                for i in range(1, res - 1):
                    temp_pts[i] = smoothed_source[i] * 0.5 + (smoothed_source[i-1] + smoothed_source[i+1]) * 0.25
                smoothed_source = temp_pts
            smoothed_source[0] = uniform_source_pts[0]
            smoothed_source[-1] = uniform_source_pts[-1]

            # 2. Calcul des tangentes
            tangents = []
            for i in range(res):
                if i == 0:
                    t_vec = smoothed_source[1] - smoothed_source[0]
                elif i == res - 1:
                    t_vec = smoothed_source[-1] - smoothed_source[-2]
                else:
                    t_vec = smoothed_source[i+1] - smoothed_source[i-1]
                
                if t_vec.length > 1e-6:
                    t_vec.normalize()
                else:
                    t_vec = Vector((0, 0, 1))
                tangents.append(t_vec)

            # 3. Construction du cadre de transport parallèle (Bishop Frame)
            normals = [None] * res
            binormals = [None] * res

            ref_up = Vector((0, 0, 1))
            if abs(tangents[0].dot(ref_up)) > 0.99:
                ref_up = Vector((0, 1, 0))
            n0 = ref_up.cross(tangents[0])
            if n0.length < 1e-4:
                n0 = Vector((1, 0, 0))
            n0.normalize()
            normals[0] = n0
            binormals[0] = tangents[0].cross(normals[0]).normalized()

            for i in range(1, res):
                rot = tangents[i-1].rotation_difference(tangents[i])
                next_n = rot @ normals[i-1]
                next_n.normalize()
                normals[i] = next_n
                binormals[i] = tangents[i].cross(next_n).normalized()

            # 4. Application de l'hélice avec fondu progressif (taper) pour fusionner proprement dans les ancres
            points = []
            for i in range(res):
                t = i / (res - 1)
                center_pos = smoothed_source[i]
                normal = normals[i]
                binormal = binormals[i]

                # Facteur de fondu (0 aux extrémités pour rejoindre l'ancre en pointe, 1 au cœur)
                taper = 1.0
                if t < 0.15:
                    taper = t / 0.15
                elif t > 0.85:
                    taper = (1.0 - t) / 0.15
                taper = max(0.0, min(1.0, taper))
                taper = taper * taper * (3.0 - 2.0 * taper)  # Lissage smoothstep

                angle = (t * turns + offset_val) * 2 * pi
                offset = (normal * cos(angle) + binormal * sin(angle)) * helix_radius * taper
                points.append(center_pos + offset)

            points[0] = p1_w.copy()
            points[-1] = p2_w.copy()
        else:
            points = [p1_w.lerp(p2_w, i / (res - 1)) for i in range(res)]
    else:
        # CAS DU CÂBLE STANDARD
        if obj.cable_initial_shape and not is_twined:
            base_points = [Vector([float(val) for val in pt.split(",")]) for pt in obj.cable_initial_shape.split(";") if pt]
        else:
            base_points = []

        if not base_points:
            base_points = [Vector((0, 0, -i)) for i in range(res)]
            obj.cable_initial_shape = ";".join([f"{v.x},{v.y},{v.z}" for v in base_points])

        p_orig_start = base_points[0]
        p_orig_end = base_points[-1]
        orig_vector = p_orig_end - p_orig_start
        orig_len = orig_vector.length

        new_vector = p2_w - p1_w
        new_len = new_vector.length

        points = []
        for pt in base_points:
            if orig_len > 1e-6:
                t = (pt - p_orig_start).dot(orig_vector) / (orig_len**2)
                t = max(0.0, min(1.0, t))
            else:
                t = 0.0

            base_interp = p1_w.lerp(p2_w, t)

            if orig_len > 1e-6:
                axis_projection = p_orig_start + orig_vector * t
                local_offset = pt - axis_projection
            else:
                local_offset = Vector((0, 0, 0))

            scale_ratio = (new_len / orig_len) if orig_len > 1e-6 else 1.0
            points.append(base_interp + (local_offset * scale_ratio))
            
        if len(points) != res:
            res_points = []
            for i in range(res):
                t = i / (res - 1)
                idx_f = t * (len(points) - 1)
                idx1 = int(idx_f)
                idx2 = min(idx1 + 1, len(points) - 1)
                frac = idx_f - idx1
                res_points.append(points[idx1].lerp(points[idx2], frac))
            points = res_points

        sag = obj.cable_sag
        coll_collection = obj.cable_colliders

        if context is None:
            context = bpy.context
            
        depsgraph = context.evaluated_depsgraph_get()
        trees = get_collider_trees(coll_collection, depsgraph, current_obj=obj)

        if trees or sag > 0.001:
            orig_arc_length = sum((base_points[i+1] - base_points[i]).length for i in range(len(base_points) - 1))
            target_total_length = max(new_len, orig_arc_length)
            target_seg_len = target_total_length / (res - 1)

            rayon_total = bevel + 0.002
            stiffness = obj.stiffness
            gravity_step = (sag * 0.5) / 50.0

            epsilon = 0.0001
            max_iterations = obj.maxiter
            iteration = 0
            converged = False

            while not converged and iteration < max_iterations:
                iteration += 1
                max_displacement = 0.0
                prev_positions = [p.copy() for p in points]

                for i in range(1, res - 1):
                    points[i].z -= gravity_step

                for _iter in range(2):
                    for i in range(res - 1):
                        p_a = points[i]
                        p_b = points[i + 1]
                        delta = p_b - p_a
                        current_len = delta.length
                        if current_len > 1e-6:
                            diff = (current_len - target_seg_len) / current_len
                            correction = delta * stiffness * diff
                            if i > 0:
                                points[i] += correction * 0.5
                            if i + 1 < res - 1:
                                points[i + 1] -= correction * 0.5

                if trees:
                    for i in range(1, res - 1):
                        p = points[i]
                        for bvh in trees:
                            loc, norm, idx, dist = bvh.find_nearest(p)
                            if loc is not None:
                                vec = p - loc
                                if vec.dot(norm) < 0 or dist < rayon_total:
                                    points[i] = loc + norm * rayon_total

                smoothed = list(points)
                for i in range(1, res - 1):
                    smoothed[i] = points[i] * 0.5 + (points[i - 1] + points[i + 1]) * 0.25
                points = smoothed

                points[0] = p1_w.copy()
                points[-1] = p2_w.copy()

                for i in range(1, res - 1):
                    movement = (points[i] - prev_positions[i]).length
                    if movement > max_displacement:
                        max_displacement = movement

                if max_displacement < epsilon:
                    converged = True

        points[0] = p1_w.copy()
        points[-1] = p2_w.copy()

    if is_twined:
        inv_matrix = obj.matrix_world.inverted()
        curve_data.bevel_depth = 0.0
        curve_data.splines.clear()
        cable_spline = curve_data.splines.new('POLY')
        cable_spline.points.add(len(points) - 1)
        for i, pos_w in enumerate(points):
            pos_loc = inv_matrix @ pos_w
            cable_spline.points[i].co = (pos_loc.x, pos_loc.y, pos_loc.z, 1.0)
        return

    # Gestion des bouts sortants pour les câbles standards
    out_a = max(0.0, obj.cable_out_a)
    out_b = max(0.0, obj.cable_out_b)
    smoothness_iters = max(0, obj.cable_smoothness)

    dir_a = Vector((0, 0, 1))
    dir_b = Vector((0, 0, 1))

    if out_a > 1e-4:
        mat_a = obj.cable_p1.matrix_world.to_3x3()
        dir_a = mat_a @ Vector((0, 0, 1))
        if dir_a.length > 1e-4:
            dir_a.normalize()
        else:
            dir_a = (p2_w - p1_w).normalized()
        
        n_out_a = max(3, int(res * 0.15))
        ext_points_a = [p1_w - dir_a * (out_a * (j / n_out_a)) for j in range(n_out_a, 0, -1)]
        points = ext_points_a + points

    if out_b > 1e-4:
        mat_b = obj.cable_p2.matrix_world.to_3x3()
        dir_b = mat_b @ Vector((0, 0, 1))
        if dir_b.length > 1e-4:
            dir_b.normalize()
        else:
            dir_b = (p2_w - p1_w).normalized()
        
        n_out_b = max(3, int(res * 0.15))
        ext_points_b = [p2_w + dir_b * (out_b * (j / n_out_b)) for j in range(1, n_out_b + 1)]
        points = points + ext_points_b

    if (out_a > 1e-4 or out_b > 1e-4) and smoothness_iters > 0:
        for _ in range(smoothness_iters):
            smoothed = list(points)
            start_idx = len(ext_points_a) if out_a > 1e-4 else 1
            end_idx = len(points) - len(ext_points_b) if out_b > 1e-4 else len(points) - 1
            
            for i in range(max(1, start_idx - 3), min(len(points) - 1, end_idx + 3)):
                smoothed[i] = points[i] * 0.4 + (points[i - 1] + points[i + 1]) * 0.3
            points = smoothed

            if out_a > 1e-4:
                points[0] = p1_w - dir_a * out_a
            if out_b > 1e-4:
                points[-1] = p2_w + dir_b * out_b

    inv_matrix = obj.matrix_world.inverted()
    curve_data.bevel_depth = 0.0

    curve_data.splines.clear()
    cable_spline = curve_data.splines.new('POLY')
    cable_spline.points.add(len(points) - 1)

    for i, pos_w in enumerate(points):
        pos_loc = inv_matrix @ pos_w
        cable_spline.points[i].co = (pos_loc.x, pos_loc.y, pos_loc.z, 1.0)


def _on_prop_update(self, context):
    update_cable_geometry(self, context)


py_last_empty_transforms = {}
py_last_curve_shapes = {}

@bpy.app.handlers.persistent
def auto_update_cables_handler(scene):
    global py_last_empty_transforms, py_last_curve_shapes
    current_empty_transforms = {}
    current_curve_shapes = {}
    curves_to_update = set()
    twined_curves_to_update = set()
    
    anchor_emptys = set()
    empty_to_curves = {}
    
    for obj in bpy.data.objects:
        if obj.type == 'CURVE':
            if obj.mode == 'EDIT':
                continue
            p1 = getattr(obj, "cable_p1", None)
            p2 = getattr(obj, "cable_p2", None)
            
            if p1 or p2:
                if p1:
                    anchor_emptys.add(p1)
                    empty_to_curves.setdefault(p1, set()).add(obj)
                if p2:
                    anchor_emptys.add(p2)
                    empty_to_curves.setdefault(p2, set()).add(obj)
                    
    for emp in anchor_emptys:
        pos = emp.matrix_world.translation.copy()
        rot = emp.matrix_world.to_quaternion()
        transform_signature = (pos.x, pos.y, pos.z, rot.w, rot.x, rot.y, rot.z)
        
        current_empty_transforms[emp.name] = transform_signature
        
        if emp.name not in py_last_empty_transforms:
            py_last_empty_transforms[emp.name] = transform_signature
        else:
            if transform_signature != py_last_empty_transforms[emp.name]:
                if emp in empty_to_curves:
                    curves_to_update.update(empty_to_curves[emp])

    for obj in bpy.data.objects:
        if obj.type == 'CURVE' and not getattr(obj, "cable_is_twined", False):
            if obj.mode == 'EDIT':
                continue
            if obj.data.splines:
                spline = obj.data.splines[0]
                pts_sig = tuple(tuple(bp.co) for bp in spline.bezier_points) if spline.type == 'BEZIER' else tuple(tuple(p.co[:3]) for p in spline.points)
                current_curve_shapes[obj.name] = pts_sig
                
                if obj.name not in py_last_curve_shapes:
                    py_last_curve_shapes[obj.name] = pts_sig
                else:
                    if pts_sig != py_last_curve_shapes[obj.name]:
                        for other_obj in bpy.data.objects:
                            if other_obj.type == 'CURVE' and getattr(other_obj, "cable_source", None) == obj:
                                twined_curves_to_update.add(other_obj)

    if curves_to_update or twined_curves_to_update:
        py_last_empty_transforms.update(current_empty_transforms)
        py_last_curve_shapes.update(current_curve_shapes)
        
        for obj in curves_to_update:
            if obj in bpy.data.objects.values() and obj.mode != 'EDIT':
                update_cable_geometry(obj)
                if obj.data.splines:
                    spline = obj.data.splines[0]
                    py_last_curve_shapes[obj.name] = tuple(tuple(bp.co) for bp in spline.bezier_points) if spline.type == 'BEZIER' else tuple(tuple(p.co[:3]) for p in spline.points)

        for obj in twined_curves_to_update:
            if obj in bpy.data.objects.values() and obj.mode != 'EDIT':
                update_cable_geometry(obj)


class CURVE_OT_setup_cable(bpy.types.Operator):
    bl_idname = "curve.setup_cable"
    bl_label = "Créer les Ancres"
    bl_description = "Crée automatiquement les objets Empty aux extrémités de la courbe sélectionnée"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'CURVE':
            self.report({'ERROR'}, "Sélectionne un objet Courbe.")
            return {'CANCELLED'}

        curve_data = obj.data
        if not curve_data.splines:
            self.report({'ERROR'}, "La courbe est vide.")
            return {'CANCELLED'}

        first_spline = curve_data.splines[0]
        inv_matrix = obj.matrix_world.inverted()
        world_points = []
        
        if first_spline.type == 'BEZIER':
            if len(first_spline.bezier_points) < 2:
                self.report({'ERROR'}, "La courbe Bézier doit avoir au moins 2 points.")
                return {'CANCELLED'}
            for bp in first_spline.bezier_points:
                world_points.append(obj.matrix_world @ Vector(bp.co))
            p1_loc = Vector(first_spline.bezier_points[0].co)
            p2_loc = Vector(first_spline.bezier_points[-1].co)
        else:
            if len(first_spline.points) < 2:
                self.report({'ERROR'}, "La courbe doit avoir au moins 2 points.")
                return {'CANCELLED'}
            for p in first_spline.points:
                world_points.append(obj.matrix_world @ Vector(p.co[:3]))
            p1_loc = Vector(first_spline.points[0].co[:3])
            p2_loc = Vector(first_spline.points[-1].co[:3])

        # Capture immédiate de la forme initiale pour éviter le bug d'affichage initial
        local_pts_str = []
        for pt_w in world_points:
            pt_loc = inv_matrix @ pt_w
            local_pts_str.append(f"{pt_loc.x},{pt_loc.y},{pt_loc.z}")
        obj.cable_initial_shape = ";".join(local_pts_str)

        p1_w = obj.matrix_world @ p1_loc
        p2_w = obj.matrix_world @ p2_loc

        obj.cable_p1 = get_or_create_unique_empty(f"{obj.name}_Ancre_A", p1_w, context)
        obj.cable_p2 = get_or_create_unique_empty(f"{obj.name}_Ancre_B", p2_w, context)

        v_dir = p2_w - p1_w
        if v_dir.length > 1e-4:
            q = v_dir.normalized().to_track_quat('Z', 'Y')
            obj.cable_p1.rotation_euler = q.to_euler()
            obj.cable_p2.rotation_euler = q.to_euler()

        update_cable_geometry(obj, context)
        self.report({'INFO'}, "Ancres créées et associées avec succès !")
        return {'FINISHED'}


class CURVE_OT_add_twined_cable(bpy.types.Operator):
    bl_idname = "curve.add_twined_cable"
    bl_label = "Ajouter Câble Entortillé"
    bl_description = "Génère un nouveau câble en spirale autour du câble sélectionné, partageant les mêmes ancres"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        sel_obj = context.active_object
        if not sel_obj or sel_obj.type != 'CURVE':
            self.report({'ERROR'}, "Sélectionne un câble source.")
            return {'CANCELLED'}

        p1 = getattr(sel_obj, "cable_p1", None)
        p2 = getattr(sel_obj, "cable_p2", None)

        if not p1 or not p2:
            self.report({'ERROR'}, "Le câble sélectionné doit avoir des ancres valides. Créez d'abord les ancres.")
            return {'CANCELLED'}

        curve_data = bpy.data.curves.new(name="TwinedCableData", type='CURVE')
        curve_data.dimensions = '3D'
        new_obj = bpy.data.objects.new(name="TwinedCable", object_data=curve_data)
        context.collection.objects.link(new_obj)

        new_obj.cable_p1 = p1
        new_obj.cable_p2 = p2
        new_obj.cable_source = sel_obj
        new_obj.cable_sag = sel_obj.cable_sag
        new_obj.stiffness = sel_obj.stiffness
        new_obj.cable_bevel = max(0.005, sel_obj.cable_bevel * 0.8)
        new_obj.cable_res = sel_obj.cable_res
        new_obj.rings = sel_obj.rings
        new_obj.maxiter = sel_obj.maxiter
        new_obj.cable_out_a = sel_obj.cable_out_a
        new_obj.cable_out_b = sel_obj.cable_out_b
        new_obj.cable_smoothness = sel_obj.cable_smoothness
        new_obj.cable_colliders = sel_obj.cable_colliders
        new_obj.cable_is_twined = True
        new_obj.cable_turns = getattr(sel_obj, "cable_turns", 8.0)
        new_obj.cable_offset = 0.0

        bpy.ops.object.select_all(action='DESELECT')
        new_obj.select_set(True)
        context.view_layer.objects.active = new_obj

        update_cable_geometry(new_obj, context)
        self.report({'INFO'}, "Câble entortillé généré avec succès autour du câble source !")
        return {'FINISHED'}


class CURVE_OT_update_initial_shape(bpy.types.Operator):
    bl_idname = "curve.update_initial_shape"
    bl_label = "Mettre à jour l'état initial"
    bl_description = "Capture la géométrie actuelle de la courbe pour l'utiliser comme nouvelle référence de simulation"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'CURVE':
            self.report({'ERROR'}, "Sélectionne un objet Courbe.")
            return {'CANCELLED'}

        curve_data = obj.data
        if not curve_data.splines:
            self.report({'ERROR'}, "La courbe est vide.")
            return {'CANCELLED'}

        first_spline = curve_data.splines[0]
        inv_matrix = obj.matrix_world.inverted()
        world_points = []
        
        if first_spline.type == 'BEZIER':
            for bp in first_spline.bezier_points:
                world_points.append(obj.matrix_world @ Vector(bp.co))
        else:
            for p in first_spline.points:
                world_points.append(obj.matrix_world @ Vector(p.co[:3]))

        if len(world_points) < 2:
            self.report({'ERROR'}, "La courbe doit avoir au moins 2 points.")
            return {'CANCELLED'}

        local_pts_str = []
        for pt_w in world_points:
            pt_loc = inv_matrix @ pt_w
            local_pts_str.append(f"{pt_loc.x},{pt_loc.y},{pt_loc.z}")

        obj.cable_initial_shape = ";".join(local_pts_str)
        update_cable_geometry(obj, context)
        
        self.report({'INFO'}, "État initial mis à jour avec la forme actuelle !")
        return {'FINISHED'}


class CURVE_OT_delete_cable(bpy.types.Operator):
    bl_idname = "curve.delete_cable"
    bl_label = "Supprimer le Câble"
    bl_description = "Supprime définitivement la courbe ainsi que ses deux ancres associées"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = context.active_object
        if not obj or obj.type != 'CURVE':
            self.report({'ERROR'}, "Sélectionne un objet Courbe valide.")
            return {'CANCELLED'}

        p1 = getattr(obj, "cable_p1", None)
        p2 = getattr(obj, "cable_p2", None)

        emptys_to_delete = []
        for emp in [p1, p2]:
            if emp:
                is_used_elsewhere = False
                for other_obj in bpy.data.objects:
                    if other_obj != obj and other_obj.type == 'CURVE':
                        if getattr(other_obj, "cable_p1", None) == emp or getattr(other_obj, "cable_p2", None) == emp:
                            is_used_elsewhere = True
                            break
                if not is_used_elsewhere:
                    emptys_to_delete.append(emp)

        bpy.data.objects.remove(obj, do_unlink=True)

        for emp in emptys_to_delete:
            bpy.data.objects.remove(emp, do_unlink=True)

        self.report({'INFO'}, "Câble et ancres supprimés avec succès.")
        return {'FINISHED'}


class VIEW3D_PT_cable_panel(bpy.types.Panel):
    bl_label = "CableGuy"
    bl_idname = "VIEW3D_PT_cable_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'CableGuy'

    def draw(self, context):
        layout = self.layout
        obj = context.active_object

        if obj and obj.type == 'CURVE':
            layout.operator("curve.setup_cable", text="Créer les Ancres", icon='EMPTY_DATA')
            
            row = layout.row()
            row.operator("curve.add_twined_cable", text="Ajouter Câble Entortillé", icon='OUTLINER_OB_CURVE')
            row.enabled = (getattr(obj, "cable_p1", None) is not None and getattr(obj, "cable_p2", None) is not None)

            layout.operator("curve.update_initial_shape", text="Mettre à jour l'état initial", icon='FILE_REFRESH')
            layout.operator("curve.delete_cable", text="Supprimer le Câble", icon='TRASH')
            
            layout.separator()
            is_twined = getattr(obj, "cable_is_twined", False)

            if not is_twined:
                layout.prop(obj, "cable_sag")
                layout.prop(obj, "stiffness")

            layout.prop(obj, "cable_bevel")
            layout.prop(obj, "cable_res")
            layout.prop(obj, "rings")

            if not is_twined:
                layout.prop(obj, "maxiter")

            if is_twined:
                layout.prop(obj, "cable_turns")
                layout.prop(obj, "cable_offset")

            if not is_twined:
                layout.separator()
                layout.label(text="Bouts Sortants (Rigidité Attaches)")
                layout.prop(obj, "cable_out_a")
                layout.prop(obj, "cable_out_b")
                layout.prop(obj, "cable_smoothness")

            if not is_twined:
                layout.separator()
                layout.prop(obj, "cable_colliders")

            layout.separator()
            layout.prop(obj, "cable_p1")
            layout.prop(obj, "cable_p2")
        else:
            layout.label(text="Sélectionne une courbe")


classes = (
    CURVE_OT_setup_cable,
    CURVE_OT_add_twined_cable,
    CURVE_OT_update_initial_shape,
    CURVE_OT_delete_cable,
    VIEW3D_PT_cable_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.Object.cable_sag = bpy.props.FloatProperty(
        name="Affaissement", default=0.8, min=0.0, soft_max=10.0, unit='LENGTH', update=_on_prop_update
    )
    bpy.types.Object.stiffness = bpy.props.FloatProperty(
            name="Stiffness", default=0.3, min=0.001, soft_max=2.0, unit='LENGTH', update=_on_prop_update
        )
    bpy.types.Object.cable_bevel = bpy.props.FloatProperty(
        name="Épaisseur", default=0.03, min=0.001, soft_max=0.5, unit='LENGTH', update=_on_prop_update
    )
    bpy.types.Object.cable_res = bpy.props.IntProperty(
        name="Résolution", default=32, min=4, max=256, update=_on_prop_update
    )
    bpy.types.Object.rings = bpy.props.IntProperty(
        name="Rings", default=8, min=0, max=256, update=_on_prop_update
    )
    bpy.types.Object.maxiter = bpy.props.IntProperty(
        name="Max iteration", default=40, min=0, max=1000, update=_on_prop_update
    )
    bpy.types.Object.cable_out_a = bpy.props.FloatProperty(
        name="Bout Sortant A", default=0.0, min=0.0, soft_max=5.0, unit='LENGTH', update=_on_prop_update
    )
    bpy.types.Object.cable_out_b = bpy.props.FloatProperty(
        name="Bout Sortant B", default=0.0, min=0.0, soft_max=5.0, unit='LENGTH', update=_on_prop_update
    )
    bpy.types.Object.cable_smoothness = bpy.props.IntProperty(
        name="Lissage Jonction", default=6, min=0, max=30, update=_on_prop_update
    )
    bpy.types.Object.cable_colliders = bpy.props.PointerProperty(
        type=bpy.types.Collection, name="Colliders", description="Collection d'objets obstacles", update=_on_prop_update
    )
    bpy.types.Object.cable_p1 = bpy.props.PointerProperty(
        type=bpy.types.Object, name="Ancre A"
    )
    bpy.types.Object.cable_p2 = bpy.props.PointerProperty(
        type=bpy.types.Object, name="Ancre B"
    )
    bpy.types.Object.cable_source = bpy.props.PointerProperty(
        type=bpy.types.Object, name="Câble Source"
    )
    bpy.types.Object.cable_initial_shape = bpy.props.StringProperty(name="Forme Initiale", default="")
    bpy.types.Object.cable_is_twined = bpy.props.BoolProperty(name="Câble Entortillé", default=False)
    bpy.types.Object.cable_turns = bpy.props.FloatProperty(name="Nombre de tours", default=8.0, min=0.1, max=100.0, update=_on_prop_update)
    bpy.types.Object.cable_offset = bpy.props.FloatProperty(name="Offset", default=0.0, min=-10.0, max=10.0, update=_on_prop_update)

    if auto_update_cables_handler not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(auto_update_cables_handler)


def unregister():
    if auto_update_cables_handler in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(auto_update_cables_handler)

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)

    del bpy.types.Object.cable_sag
    del bpy.types.Object.stiffness
    del bpy.types.Object.cable_bevel
    del bpy.types.Object.cable_res
    del bpy.types.Object.rings
    del bpy.types.Object.maxiter
    del bpy.types.Object.cable_out_a
    del bpy.types.Object.cable_out_b
    del bpy.types.Object.cable_smoothness
    del bpy.types.Object.cable_colliders
    del bpy.types.Object.cable_p1
    del bpy.types.Object.cable_p2
    del bpy.types.Object.cable_source
    del bpy.types.Object.cable_initial_shape
    del bpy.types.Object.cable_is_twined
    del bpy.types.Object.cable_turns
    del bpy.types.Object.cable_offset


if __name__ == "__main__":
    register()