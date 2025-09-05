# SPDX-License-Identifier: GPL-2.0-or-later
bl_info = {
    "name": "Animation Offset Duplicator + Partial Cycle",
    "blender": (4, 5, 0),
    "category": "Object",
    "author": "ChatGPT",
    "version": (1, 3, 0),
    "description": "Duplicate objects with offset & randomness. Includes Cycles config. Adds Partial Cycle tool for repeating a keyframe sub-range with pre/post roll. Now offsets shapekey (Key) animation as well.",
}

import bpy
import uuid
import math
import random
from mathutils import Vector, Euler
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import IntProperty, EnumProperty, BoolProperty, FloatProperty, PointerProperty

# --- Keys ---
AOD_GROUP_KEY = "aod_group_id"
AOD_IS_SOURCE_KEY = "aod_is_source"
AOD_INDEX_KEY = "aod_index"

# --- Utils ---
def _object_has_obj_action(obj):
    return (obj and obj.animation_data and obj.animation_data.action)

def _get_shapekey_data(obj):
    try:
        return obj.data.shape_keys if (obj and obj.data and getattr(obj.data, "shape_keys", None)) else None
    except Exception:
        return None

def _object_has_shapekey_action(obj):
    sk = _get_shapekey_data(obj)
    return (sk and sk.animation_data and sk.animation_data.action)

def _object_has_any_action(obj):
    return _object_has_obj_action(obj) or _object_has_shapekey_action(obj)


def _iter_actions_for_object(obj):
    """Yield tuples of (owner_id, action, kind) for both Object and Key datablocks."""
    if _object_has_obj_action(obj):
        yield (obj, obj.animation_data.action, 'OBJECT')
    sk = _get_shapekey_data(obj)
    if sk and sk.animation_data and sk.animation_data.action:
        yield (sk, sk.animation_data.action, 'SHAPEKEY')


def _offset_action_keyframes_in_time(action: bpy.types.Action, dx: float):
    if not action or dx == 0:
        return
    for fcu in action.fcurves:
        for kp in fcu.keyframe_points:
            kp.co.x += dx
            kp.handle_left.x += dx
            kp.handle_right.x += dx
        fcu.update()


def _apply_cycles_modifier(action: bpy.types.Action, s):
    if not action:
        return
    for fcu in action.fcurves:
        cyc = next((m for m in fcu.modifiers if m.type == 'CYCLES'), None)
        if not cyc:
            cyc = fcu.modifiers.new(type='CYCLES')
        cyc.mode_before = s.mode_before
        cyc.mode_after  = s.mode_after
        cyc.cycles_before = max(0, s.cycles_before)   # 0 = infinite
        cyc.cycles_after  = max(0, s.cycles_after)    # 0 = infinite
        cyc.use_influence = s.use_influence
        cyc.influence     = s.influence
        cyc.use_restricted_range = s.use_restricted_range
        if s.use_restricted_range:
            cyc.frame_start = s.frame_start
            cyc.frame_end   = s.frame_end
            cyc.blend_in    = max(0.0, s.blend_in)
            cyc.blend_out   = max(0.0, s.blend_out)
        else:
            cyc.blend_in  = 0.0
            cyc.blend_out = 0.0


def _rand_between(a: float, b: float) -> float:
    lo, hi = (a, b) if a <= b else (b, a)
    return random.uniform(lo, hi)


def _clear_delta_transforms(obj):
    obj.delta_location = Vector((0.0, 0.0, 0.0))
    if obj.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        obj.delta_rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    else:
        obj.delta_rotation_euler = Euler((0.0, 0.0, 0.0), 'XYZ')
    obj.delta_scale = Vector((1.0, 1.0, 1.0))


def _apply_random_deltas(obj, s):
    _clear_delta_transforms(obj)
    if not s.add_randomness:
        return 0.0
    tx = _rand_between(s.tx_min, s.tx_max)
    ty = _rand_between(s.ty_min, s.ty_max)
    tz = _rand_between(s.tz_min, s.tz_max)
    obj.delta_location = Vector((tx, ty, tz))
    rx = math.radians(_rand_between(s.rx_min, s.rx_max))
    ry = math.radians(_rand_between(s.ry_min, s.ry_max))
    rz = math.radians(_rand_between(s.rz_min, s.rz_max))
    if obj.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        obj.delta_rotation_quaternion = Euler((rx, ry, rz), 'XYZ').to_quaternion()
    else:
        obj.delta_rotation_euler = Euler((rx, ry, rz), 'XYZ')
    sx = _rand_between(s.sx_min, s.sx_max)
    sy = _rand_between(s.sy_min, s.sy_max)
    sz = _rand_between(s.sz_min, s.sz_max)
    obj.delta_scale = Vector((sx, sy, sz))
    return _rand_between(s.frame_jitter_min, s.frame_jitter_max)


def _ensure_group_for_source(src) -> str:
    gid = src.get(AOD_GROUP_KEY)
    if not gid:
        gid = str(uuid.uuid4())
        src[AOD_GROUP_KEY] = gid
        src[AOD_IS_SOURCE_KEY] = True
    return gid


def _desired_collection_name_for_source(src) -> str:
    return f"{src.name}_dups"


def _get_collection_by_group_id(group_id: str):
    for coll in bpy.data.collections:
        if coll.get(AOD_GROUP_KEY) == group_id:
            return coll
    return None


def _hard_delete_collection(coll) -> int:
    if not coll:
        return 0
    to_delete = list(coll.objects)
    deleted = 0
    for ob in to_delete:
        try:
            bpy.data.objects.remove(ob, do_unlink=True)
            deleted += 1
        except Exception:
            try:
                for c in list(ob.users_collection):
                    c.objects.unlink(ob)
                bpy.data.objects.remove(ob, do_unlink=True)
                deleted += 1
            except Exception:
                pass
    try:
        bpy.data.collections.remove(coll, do_unlink=True)
    except Exception:
        try:
            bpy.context.scene.collection.children.unlink(coll)
            bpy.data.collections.remove(coll, do_unlink=True)
        except Exception:
            pass
    return deleted


def _cleanup_orphan_actions():
    for act in list(bpy.data.actions):
        if not act.use_fake_user and act.users == 0:
            try:
                bpy.data.actions.remove(act)
            except Exception:
                pass

# --- Properties ---
class AOD_Settings(PropertyGroup):
    copies: IntProperty(name="Copies", default=5, min=1)
    frame_offset: IntProperty(name="Frame Offset", default=10)
    use_instances: BoolProperty(name="Use Instances", default=True)
    mode_items = [
        ('NONE',          "No Cycles", "Do not repeat"),
        ('REPEAT',        "Repeat Motion", "Repeat without value offset"),
        ('REPEAT_OFFSET', "Repeat With Offset", "Repeat with additive value offset"),
        ('MIRROR',        "Repeat Mirrored", "Flip each cycle across X"),
    ]
    mode_before: EnumProperty(name="Before", items=mode_items, default='NONE')
    mode_after:  EnumProperty(name="After", items=mode_items, default='REPEAT')
    cycles_before: IntProperty(name="Cycles Before", default=0, min=0, description="0 = infinite")
    cycles_after:  IntProperty(name="Cycles After", default=0, min=0, description="0 = infinite")
    apply_to_original: BoolProperty(name="Apply to Original", default=False)
    use_influence: BoolProperty(name="Use Influence", default=False)
    influence: FloatProperty(name="Influence", default=1.0, min=0.0, max=1.0)
    use_restricted_range: BoolProperty(name="Restrict Frame Range", default=False)
    frame_start: IntProperty(name="Start", default=1)
    frame_end:   IntProperty(name="End",   default=250)
    blend_in:  FloatProperty(name="Blend In",  default=0.0, min=0.0)
    blend_out: FloatProperty(name="Blend Out", default=0.0, min=0.0)
    add_randomness: BoolProperty(name="Add Randomness", default=False)
    frame_jitter_min: IntProperty(name="Frame Jitter Min", default=0)
    frame_jitter_max: IntProperty(name="Frame Jitter Max", default=0)
    tx_min: FloatProperty(name="Tx Min", default=0.0); tx_max: FloatProperty(name="Tx Max", default=0.0)
    ty_min: FloatProperty(name="Ty Min", default=0.0); ty_max: FloatProperty(name="Ty Max", default=0.0)
    tz_min: FloatProperty(name="Tz Min", default=0.0); tz_max: FloatProperty(name="Tz Max", default=0.0)
    rx_min: FloatProperty(name="Rx Min (°)", default=0.0); rx_max: FloatProperty(name="Rx Max (°)", default=0.0)
    ry_min: FloatProperty(name="Ry Min (°)", default=0.0); ry_max: FloatProperty(name="Ry Max (°)", default=0.0)
    rz_min: FloatProperty(name="Rz Min (°)", default=0.0); rz_max: FloatProperty(name="Rz Max (°)", default=0.0)
    sx_min: FloatProperty(name="Sx Min", default=1.0); sx_max: FloatProperty(name="Sx Max", default=1.0)
    sy_min: FloatProperty(name="Sy Min", default=1.0); sy_max: FloatProperty(name="Sy Max", default=1.0)
    sz_min: FloatProperty(name="Sz Min", default=1.0); sz_max: FloatProperty(name="Sz Max", default=1.0)

# --- Operator ---
class AOD_OT_recreate(Operator):
    bl_idname = "object.aod_recreate_duplicates"
    bl_label = "Create / Recreate Duplicates"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.aod_settings
        src = context.active_object
        if not src:
            self.report({'ERROR'}, "No active object.")
            return {'CANCELLED'}
        if not _object_has_any_action(src):
            self.report({'ERROR'}, "Source must have an Action on the Object and/or its Shapekeys.")
            return {'CANCELLED'}

        # If there is shapekey animation and use_instances is ON, per-duplicate timing would
        # otherwise affect all instances (Key action targets the Key ID). In that case we silently
        # force data copy for the duplicates to give each its own Key datablock.
        sk_has_action = _object_has_shapekey_action(src)

        gid = _ensure_group_for_source(src)

        old_coll = _get_collection_by_group_id(gid)
        deleted = _hard_delete_collection(old_coll)
        _cleanup_orphan_actions()

        desired_name = _desired_collection_name_for_source(src)
        new_coll = bpy.data.collections.new(desired_name)
        new_coll[AOD_GROUP_KEY] = gid
        bpy.context.scene.collection.children.link(new_coll)

        # Apply cycles to original actions if requested
        if s.apply_to_original:
            for owner, action, _kind in _iter_actions_for_object(src):
                _apply_cycles_modifier(action, s)

        made = 0
        base_obj_action = src.animation_data.action if _object_has_obj_action(src) else None
        base_sk_action = _get_shapekey_data(src).animation_data.action if sk_has_action else None

        for i in range(1, s.copies + 1):
            dup = src.copy()
            dup.name = f"{src.name}_dup_{i:02d}"

            # Force unique mesh data if shapekey animation exists so we can offset per duplicate
            force_unique_data = sk_has_action
            if s.use_instances and not force_unique_data:
                dup.data = (src.data if s.use_instances else (src.data.copy() if src.data else None))
            else:
                dup.data = (src.data.copy() if src.data else None)

            for c in list(dup.users_collection):
                c.objects.unlink(dup)
            new_coll.objects.link(dup)
            dup[AOD_GROUP_KEY] = gid
            dup[AOD_INDEX_KEY] = i

            # OBJECT action copy/offset
            if base_obj_action:
                dup.animation_data_create()
                act_obj = base_obj_action.copy()
                act_obj.name = f"{base_obj_action.name}_dup_{i:02d}"
                dup.animation_data.action = act_obj
            else:
                act_obj = None

            # SHAPEKEY action copy/offset
            act_sk = None
            if base_sk_action:
                sk = _get_shapekey_data(dup)
                if sk:
                    if not sk.animation_data:
                        sk.animation_data_create()
                    act_sk = base_sk_action.copy()
                    act_sk.name = f"{base_sk_action.name}_dup_{i:02d}"
                    sk.animation_data.action = act_sk

            # Random transform deltas + frame jitter
            jitter = _apply_random_deltas(dup, s) if s.add_randomness else (_clear_delta_transforms(dup) or 0.0)
            dx = s.frame_offset * i + jitter
            if dx:
                if act_obj:
                    _offset_action_keyframes_in_time(act_obj, dx)
                if act_sk:
                    _offset_action_keyframes_in_time(act_sk, dx)

            # Apply cycles modifiers to both actions
            if act_obj:
                _apply_cycles_modifier(act_obj, s)
            if act_sk:
                _apply_cycles_modifier(act_sk, s)

            made += 1

        new_coll.name = _desired_collection_name_for_source(src)
        msg = f"Deleted {deleted} old duplicates. Created {made} new duplicates in '{new_coll.name}'."
        if sk_has_action and s.use_instances:
            msg += " Note: Shapekey animation detected; duplicates use unique mesh data to allow per-duplicate timing."
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class AOD_OT_done(Operator):
    bl_idname = "object.aod_finish_group"
    bl_label = "Done"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        active = context.active_object
        if not active:
            self.report({'ERROR'}, "No active object.")
            return {'CANCELLED'}
        gid = active.get(AOD_GROUP_KEY)
        for ob in bpy.data.objects:
            if ob.get(AOD_GROUP_KEY) == gid:
                for k in (AOD_GROUP_KEY, AOD_IS_SOURCE_KEY, AOD_INDEX_KEY):
                    if k in ob: del ob[k]
        if gid and AOD_GROUP_KEY in active:
            del active[AOD_GROUP_KEY]
        if AOD_IS_SOURCE_KEY in active:
            del active[AOD_IS_SOURCE_KEY]
        self.report({'INFO'}, "Cleared group tags (objects/collection kept).")
        return {'FINISHED'}


# --- Partial Cycle (standalone) ---
def _collect_points_in_range(fcu, f_start, f_end):
    pts = []
    for kp in fcu.keyframe_points:
        fr = kp.co.x
        if f_start <= fr <= f_end:
            pts.append(kp)
    return pts


def _insert_points(fcu, baked_points):
    for (val, hly, hry, frame, hlx, hrx) in baked_points:
        new_pt = fcu.keyframe_points.insert(frame=frame, value=val, options={'FAST'})
        new_pt.handle_left.x = hlx
        new_pt.handle_left.y = hly
        new_pt.handle_right.x = hrx
        new_pt.handle_right.y = hry
    fcu.update()


def _evaluate_delta(fcu, f_start, f_end):
    try:
        v0 = fcu.evaluate(f_start)
        v1 = fcu.evaluate(f_end)
        return (v1 - v0)
    except Exception:
        return 0.0


class PCycleProps(PropertyGroup):
    frame_start: IntProperty(name="Start", default=1)
    frame_end: IntProperty(name="End", default=5)
    repeats: IntProperty(name="Repeats", default=3, min=1)
    roll_mode: EnumProperty(
        name="Roll",
        items=(('PREROLL', "Preroll (Left)", ""),
               ('POSTROLL', "Post-roll (Right)", "")),
        default='POSTROLL',
    )
    repeat_mode: EnumProperty(
        name="Repeat Mode",
        items=(('REPEAT', "Repeat", "Duplicate keys exactly"),
               ('REPEAT_OFFSET', "Repeat With Offset", "Add delta each cycle"),
               ('MIRROR', "Mirror", "Alternate mirrored repeats")),
        default='REPEAT_OFFSET',
    )
    clamp_to_integer_frames: BoolProperty(name="Clamp to Integer Frames", default=True)


class PCYCLE_OT_duplicate_with_offset(Operator):
    bl_idname = "pcycle.duplicate_with_offset"
    bl_label = "Duplicate Range"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.pcycle_props
        f0, f1 = sorted((s.frame_start, s.frame_end))
        length = f1 - f0
        if length <= 0:
            self.report({'ERROR'}, "Invalid range.")
            return {'CANCELLED'}

        total_inserted = 0
        for ob in context.selected_objects:  # always process all selected
            # Process Object action and Shapekey action if present
            actions = list(_iter_actions_for_object(ob))
            if not actions:
                continue
            for owner, action, _kind in actions:
                for fcu in action.fcurves:
                    src_pts = _collect_points_in_range(fcu, f0, f1)
                    if not src_pts:
                        continue
                    baked_src = [(kp.co.x, kp.co.y,
                                  kp.handle_left.x, kp.handle_left.y,
                                  kp.handle_right.x, kp.handle_right.y)
                                 for kp in src_pts]
                    delta = _evaluate_delta(fcu, f0, f1)

                    for i in range(1, s.repeats + 1):
                        if s.roll_mode == 'POSTROLL':
                            time_shift = i * length
                        else:
                            time_shift = -i * length

                        # Value shift / transform
                        if s.repeat_mode == 'REPEAT':
                            val_shift = 0
                        elif s.repeat_mode == 'REPEAT_OFFSET':
                            val_shift = (i if s.roll_mode == 'POSTROLL' else -i) * delta
                        elif s.repeat_mode == 'MIRROR':
                            sign = -1 if (i % 2) else 1
                            val_shift = sign * delta
                        else:
                            val_shift = 0

                        baked_points = []
                        for (x, y, hlx, hly, hrx, hry) in baked_src:
                            nx = x + time_shift
                            hlx2 = hlx + time_shift
                            hrx2 = hrx + time_shift
                            if s.clamp_to_integer_frames:
                                nx, hlx2, hrx2 = round(nx), round(hlx2), round(hrx2)
                            baked_points.append((y + val_shift, hly + val_shift,
                                                 hry + val_shift, nx, hlx2, hrx2))
                        _insert_points(fcu, baked_points)
                        total_inserted += len(baked_points)

        self.report({'INFO'}, f"Inserted {total_inserted} keyframes.")
        return {'FINISHED'}


# --- UI ---
class AOD_PT_panel(Panel):
    bl_label = "Animation Offset Duplicator"
    bl_idname = "AOD_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Animation"

    def draw(self, context):
        s = context.scene.aod_settings
        layout = self.layout
        col = layout.column(align=True)
        col.prop(s, "copies"); col.prop(s, "frame_offset"); col.prop(s, "use_instances")

        layout.separator(factor=0.5)
        layout.label(text="Cycles Modifier:")
        row = layout.row(align=True); row.prop(s, "mode_before"); row.prop(s, "mode_after")
        row = layout.row(align=True); row.prop(s, "cycles_before"); row.prop(s, "cycles_after")
        layout.prop(s, "apply_to_original")

        layout.prop(s, "use_influence")
        if s.use_influence: layout.prop(s, "influence")

        layout.prop(s, "use_restricted_range")
        if s.use_restricted_range:
            r = layout.row(align=True); r.prop(s, "frame_start"); r.prop(s, "frame_end")
            r = layout.row(align=True); r.prop(s, "blend_in"); r.prop(s, "blend_out")

        layout.separator(factor=0.5)
        layout.prop(s, "add_randomness")
        if s.add_randomness:
            box = layout.box()
            r = box.row(align=True); r.prop(s, "frame_jitter_min"); r.prop(s, "frame_jitter_max")
            r = box.row(align=True); r.prop(s, "tx_min"); r.prop(s, "tx_max")
            r = box.row(align=True); r.prop(s, "ty_min"); r.prop(s, "ty_max")
            r = box.row(align=True); r.prop(s, "tz_min"); r.prop(s, "tz_max")
            r = box.row(align=True); r.prop(s, "rx_min"); r.prop(s, "rx_max")
            r = box.row(align=True); r.prop(s, "ry_min"); r.prop(s, "ry_max")
            r = box.row(align=True); r.prop(s, "rz_min"); r.prop(s, "rz_max")
            r = box.row(align=True); r.prop(s, "sx_min"); r.prop(s, "sx_max")
            r = box.row(align=True); r.prop(s, "sy_min"); r.prop(s, "sy_max")
            r = box.row(align=True); r.prop(s, "sz_min"); r.prop(s, "sz_max")

        layout.separator()
        row = layout.row(align=True)
        row.operator("object.aod_recreate_duplicates", icon='DUPLICATE', text="Create / Recreate Duplicates")
        row.operator("object.aod_finish_group", icon='CHECKMARK', text="Done")

        # --- Partial Cycle UI ---
        layout.separator(factor=1.0)
        box = layout.box()
        box.label(text="Partial Cycle (Offset)")
        s2 = context.scene.pcycle_props
        row = box.row(align=True)
        row.prop(s2, "frame_start"); row.prop(s2, "frame_end")
        box.prop(s2, "roll_mode", text="Roll Mode")
        box.prop(s2, "repeat_mode", text="Mode")   # dropdown
        row = box.row(align=True)
        row.prop(s2, "repeats"); row.prop(s2, "clamp_to_integer_frames")
        box.operator("pcycle.duplicate_with_offset", icon="KEYFRAME")


# --- Registration ---
classes = (AOD_Settings, AOD_OT_recreate, AOD_OT_done,
           PCycleProps, PCYCLE_OT_duplicate_with_offset,
           AOD_PT_panel)

def register():
    for cls in classes: bpy.utils.register_class(cls)
    bpy.types.Scene.aod_settings = PointerProperty(type=AOD_Settings)
    bpy.types.Scene.pcycle_props = PointerProperty(type=PCycleProps)


def unregister():
    del bpy.types.Scene.aod_settings
    del bpy.types.Scene.pcycle_props
    for cls in reversed(classes): bpy.utils.unregister_class(cls)

if __name__ == "__main__":
    register()
