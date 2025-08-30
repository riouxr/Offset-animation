bl_info = {
    "name": "Animation Offset Duplicator (Recreate Clean via Collection)",
    "blender": (4, 5, 0),
    "category": "Object",
    "author": "ChatGPT",
    "version": (1, 19, 0),
    "description": "Recreate duplicates cleanly by placing them in a dedicated collection and deleting that collection on rerun. Includes Cycles config, randomness via Delta transforms, Instances, and Done.",
}

import bpy
import uuid
import math
import random
from mathutils import Vector, Euler
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import IntProperty, EnumProperty, BoolProperty, FloatProperty, PointerProperty

AOD_GROUP_KEY = "aod_group_id"
AOD_IS_SOURCE_KEY = "aod_is_source"
AOD_INDEX_KEY = "aod_index"
AOD_COLL_PREFIX = "AOD_"  # collection name prefix

# ---------------- Utils ----------------

def _object_has_action(obj):
    return (obj and obj.animation_data and obj.animation_data.action)

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
    # Delta translation
    tx = _rand_between(s.tx_min, s.tx_max)
    ty = _rand_between(s.ty_min, s.ty_max)
    tz = _rand_between(s.tz_min, s.tz_max)
    obj.delta_location = Vector((tx, ty, tz))
    # Delta rotation
    import math
    rx = math.radians(_rand_between(s.rx_min, s.rx_max))
    ry = math.radians(_rand_between(s.ry_min, s.ry_max))
    rz = math.radians(_rand_between(s.rz_min, s.rz_max))
    if obj.rotation_mode in {'QUATERNION', 'AXIS_ANGLE'}:
        obj.delta_rotation_quaternion = Euler((rx, ry, rz), 'XYZ').to_quaternion()
    else:
        obj.delta_rotation_euler = Euler((rx, ry, rz), 'XYZ')
    # Delta scale
    sx = _rand_between(s.sx_min, s.sx_max)
    sy = _rand_between(s.sy_min, s.sy_max)
    sz = _rand_between(s.sz_min, s.sz_max)
    obj.delta_scale = Vector((sx, sy, sz))
    # Time jitter
    return _rand_between(s.frame_jitter_min, s.frame_jitter_max)

def _ensure_group_for_source(src) -> str:
    group_id = src.get(AOD_GROUP_KEY)
    if not group_id:
        group_id = str(uuid.uuid4())
        src[AOD_GROUP_KEY] = group_id
        src[AOD_IS_SOURCE_KEY] = True
    return group_id

def _collection_name(group_id: str) -> str:
    return f"{AOD_COLL_PREFIX}{group_id[:8]}"

def _get_group_collection(group_id: str):
    name = _collection_name(group_id)
    return bpy.data.collections.get(name)

def _create_group_collection(group_id: str):
    name = _collection_name(group_id)
    coll = bpy.data.collections.new(name)
    # Put it beside the source collection: link to scene root so it's visible everywhere.
    bpy.context.scene.collection.children.link(coll)
    return coll

def _hard_delete_group_collection(group_id: str) -> int:
    """Delete the AOD collection and its objects, regardless of context."""
    coll = _get_group_collection(group_id)
    if not coll:
        return 0
    # Remove all objects in that collection (datablock remove, unlinks from all other collections too)
    to_delete = list(coll.objects)
    deleted = 0
    for ob in to_delete:
        try:
            bpy.data.objects.remove(ob, do_unlink=True)
            deleted += 1
        except Exception:
            # fallback: unlink then remove
            try:
                for c in list(ob.users_collection):
                    c.objects.unlink(ob)
                bpy.data.objects.remove(ob, do_unlink=True)
                deleted += 1
            except Exception:
                pass
    # Finally remove the collection itself
    try:
        bpy.data.collections.remove(coll, do_unlink=True)
    except Exception:
        # If some view layer still holds it, try unlink from scene then remove
        try:
            bpy.context.scene.collection.children.unlink(coll)
            bpy.data.collections.remove(coll, do_unlink=True)
        except Exception:
            pass
    return deleted

def _cleanup_orphan_actions():
    # Remove unused actions we may have created
    for act in list(bpy.data.actions):
        if not act.use_fake_user and act.users == 0:
            try:
                bpy.data.actions.remove(act)
            except Exception:
                pass

# ---------------- Properties ----------------

class AOD_Settings(PropertyGroup):
    copies: IntProperty(name="Copies", default=5, min=1)
    frame_offset: IntProperty(name="Frame Offset", default=10)
    use_instances: BoolProperty(
        name="Use Instances",
        description="Linked data (Alt+D style). Off = full copy",
        default=True,
    )
    # Cycles modes
    mode_items = [
        ('NONE',          "No Cycles",          "Do not repeat"),
        ('REPEAT',        "Repeat Motion",      "Repeat without value offset"),
        ('REPEAT_OFFSET', "Repeat With Offset", "Repeat with additive value offset"),
        ('MIRROR',        "Repeat Mirrored",    "Flip each cycle across X"),
    ]
    mode_before: EnumProperty(name="Before", items=mode_items, default='NONE')
    mode_after:  EnumProperty(name="After",  items=mode_items, default='REPEAT')
    cycles_before: IntProperty(name="Cycles Before", default=0, min=0, description="0 = infinite")
    cycles_after:  IntProperty(name="Cycles After",  default=0, min=0, description="0 = infinite")
    apply_to_original: BoolProperty(name="Apply to Original", default=False)
    use_influence: BoolProperty(name="Use Influence", default=False)
    influence: FloatProperty(name="Influence", default=1.0, min=0.0, max=1.0)
    use_restricted_range: BoolProperty(name="Restrict Frame Range", default=False)
    frame_start: IntProperty(name="Start", default=1)
    frame_end:   IntProperty(name="End",   default=250)
    blend_in:  FloatProperty(name="Blend In",  default=0.0, min=0.0)
    blend_out: FloatProperty(name="Blend Out", default=0.0, min=0.0)
    # Randomness
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

# ---------------- Operator ----------------

class AOD_OT_recreate(Operator):
    bl_idname = "object.aod_recreate_duplicates"
    bl_label = "Create / Recreate Duplicates"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.aod_settings
        active = context.active_object
        if active is None:
            self.report({'ERROR'}, "No active object.")
            return {'CANCELLED'}

        # Resolve source
        src = active
        if not _object_has_action(src):
            self.report({'ERROR'}, "Source object must have an Action with keyframes.")
            return {'CANCELLED'}

        # Ensure/obtain group id on the source
        group_id = _ensure_group_for_source(src)

        # If a previous AOD collection exists, hard-delete it (and its objects)
        deleted = _hard_delete_group_collection(group_id)
        _cleanup_orphan_actions()

        # Recreate a fresh AOD collection
        aod_coll = _create_group_collection(group_id)

        base_action = src.animation_data.action

        # Optionally apply cycles to the original (no time shift)
        if s.apply_to_original:
            _apply_cycles_modifier(base_action, s)

        # Build duplicates inside the AOD collection ONLY
        made = 0
        for i in range(1, s.copies + 1):
            dup = src.copy()
            dup.name = f"{src.name}_dup_{i:02d}"
            dup.data = (src.data if s.use_instances else (src.data.copy() if src.data else None))

            # Make sure dup is ONLY in the AOD collection
            # (avoid auto-linking to the same collection as the source)
            for c in list(dup.users_collection):
                c.objects.unlink(dup)
            aod_coll.objects.link(dup)

            # Tag for completeness (not required for cleanup now)
            dup[AOD_GROUP_KEY] = group_id
            dup[AOD_INDEX_KEY] = i

            # Per-dup action copy with time offset
            dup.animation_data_create()
            act = base_action.copy()
            act.name = f"{base_action.name}_dup_{i:02d}"
            dup.animation_data.action = act

            jitter = _apply_random_deltas(dup, s) if s.add_randomness else (_clear_delta_transforms(dup) or 0.0)
            dx = s.frame_offset * i + jitter
            if dx:
                _offset_action_keyframes_in_time(act, dx)

            _apply_cycles_modifier(act, s)

            made += 1

        self.report({'INFO'}, f"Deleted {deleted} previous duplicates. Created {made} new duplicates in {_collection_name(group_id)}.")
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
        # Clear tags on source & dups; leave objects/collection intact
        # (You can delete the AOD_ collection manually later if needed)
        for ob in bpy.data.objects:
            if ob.get(AOD_GROUP_KEY) == gid:
                for key in (AOD_GROUP_KEY, AOD_IS_SOURCE_KEY, AOD_INDEX_KEY):
                    if key in ob: del ob[key]
        if gid and AOD_GROUP_KEY in active:
            del active[AOD_GROUP_KEY]
        if AOD_IS_SOURCE_KEY in active:
            del active[AOD_IS_SOURCE_KEY]
        self.report({'INFO'}, "Cleared group tags.")
        return {'FINISHED'}

# ---------------- UI ----------------

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
        if s.use_influence:
            layout.prop(s, "influence")

        layout.prop(s, "use_restricted_range")
        if s.use_restricted_range:
            r = layout.row(align=True); r.prop(s, "frame_start"); r.prop(s, "frame_end")
            r = layout.row(align=True); r.prop(s, "blend_in"); r.prop(s, "blend_out")

        layout.separator(factor=0.5)
        layout.prop(s, "add_randomness")
        if s.add_randomness:
            box = layout.box()
            box.label(text="Time Jitter (frames):")
            r = box.row(align=True); r.prop(s, "frame_jitter_min"); r.prop(s, "frame_jitter_max")
            box.label(text="Translation (Delta):")
            r = box.row(align=True); r.prop(s, "tx_min"); r.prop(s, "tx_max")
            r = box.row(align=True); r.prop(s, "ty_min"); r.prop(s, "ty_max")
            r = box.row(align=True); r.prop(s, "tz_min"); r.prop(s, "tz_max")
            box.label(text="Rotation (Delta, degrees):")
            r = box.row(align=True); r.prop(s, "rx_min"); r.prop(s, "rx_max")
            r = box.row(align=True); r.prop(s, "ry_min"); r.prop(s, "ry_max")
            r = box.row(align=True); r.prop(s, "rz_min"); r.prop(s, "rz_max")
            box.label(text="Scale (Delta factors):")
            r = box.row(align=True); r.prop(s, "sx_min"); r.prop(s, "sx_max")
            r = box.row(align=True); r.prop(s, "sy_min"); r.prop(s, "sy_max")
            r = box.row(align=True); r.prop(s, "sz_min"); r.prop(s, "sz_max")

        layout.separator()
        row = layout.row(align=True)
        row.operator("object.aod_recreate_duplicates", icon='DUPLICATE', text="Create / Recreate Duplicates")
        row.operator("object.aod_finish_group", icon='CHECKMARK', text="Done")

# ---------------- Registration ----------------

classes = (AOD_Settings, AOD_OT_recreate, AOD_OT_done, AOD_PT_panel)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.aod_settings = PointerProperty(type=AOD_Settings)

def unregister():
    try:
        del bpy.types.Scene.aod_settings
    except Exception:
        pass
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except Exception:
            pass

if __name__ == "__main__":
    register()
