bl_info = {
    "name": "Animation Offset Duplicator (Cycles Configurable)",
    "blender": (4, 5, 0),
    "category": "Object",
    "author": "ChatGPT",
    "version": (1, 9, 0),
    "description": "Duplicate an animated object N times with keyframe time offsets, "
                   "and configure Cycles modifiers (before/after, counts, influence, restricted range with blend in/out). "
                   "Optionally apply the Cycles settings to the original object too.",
}

import bpy
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import (
    IntProperty,
    EnumProperty,
    BoolProperty,
    FloatProperty,
    PointerProperty,
)


# ------------------------- Utilities -------------------------

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

def _apply_cycles_modifier(action: bpy.types.Action, settings):
    """Create/ensure a Cycles modifier on each F-Curve and configure it per settings."""
    if not action:
        return
    for fcu in action.fcurves:
        cyc = next((m for m in fcu.modifiers if m.type == 'CYCLES'), None)
        if not cyc:
            cyc = fcu.modifiers.new(type='CYCLES')

        # Modes
        cyc.mode_before = settings.mode_before
        cyc.mode_after  = settings.mode_after

        # Counts (Blender 4.5: 0 = infinite)
        cyc.cycles_before = max(0, settings.cycles_before)
        cyc.cycles_after  = max(0, settings.cycles_after)

        # Influence
        cyc.use_influence = settings.use_influence
        cyc.influence     = settings.influence

        # Restricted range + blends
        cyc.use_restricted_range = settings.use_restricted_range
        if settings.use_restricted_range:
            cyc.frame_start = settings.frame_start
            cyc.frame_end   = settings.frame_end
            cyc.blend_in    = max(0.0, settings.blend_in)
            cyc.blend_out   = max(0.0, settings.blend_out)
        else:
            # Ensure no unexpected fades if range is off
            cyc.blend_in  = 0.0
            cyc.blend_out = 0.0


# ------------------------- Properties -------------------------

class AOD_Settings(PropertyGroup):
    # Duplication
    copies: IntProperty(
        name="Copies",
        description="How many duplicates to create",
        default=5, min=1,
    )
    frame_offset: IntProperty(
        name="Frame Offset",
        description="Time offset (in frames) added per copy (i × offset)",
        default=10,
    )

    # Cycles modes
    mode_items = [
        ('NONE',          "No Cycles",          "Do not repeat"),
        ('REPEAT',        "Repeat Motion",      "Repeat without value offset"),
        ('REPEAT_OFFSET', "Repeat With Offset", "Repeat with additive value offset"),
        ('MIRROR',        "Repeat Mirrored",    "Flip each cycle across X axis"),
    ]
    mode_before: EnumProperty(
        name="Before", description="Cycles mode before first keyframe",
        items=mode_items, default='NONE',
    )
    mode_after: EnumProperty(
        name="After", description="Cycles mode after last keyframe",
        items=mode_items, default='REPEAT',
    )

    # Cycle counts (0 = infinite)
    cycles_before: IntProperty(
        name="Cycles Before",
        description="Number of cycles before (0 = infinite)",
        default=0, min=0,
    )
    cycles_after: IntProperty(
        name="Cycles After",
        description="Number of cycles after (0 = infinite)",
        default=0, min=0,
    )

    # Apply to original
    apply_to_original: BoolProperty(
        name="Apply to Original",
        description="Also apply the Cycles settings to the original object's Action",
        default=False,
    )

    # Influence
    use_influence: BoolProperty(
        name="Use Influence",
        description="Enable blending influence",
        default=False,
    )
    influence: FloatProperty(
        name="Influence",
        description="Modifier influence (0–1)",
        default=1.0, min=0.0, max=1.0,
    )

    # Restricted frame range + blends
    use_restricted_range: BoolProperty(
        name="Restrict Frame Range",
        description="Limit modifier to a frame range",
        default=False,
    )
    frame_start: IntProperty(
        name="Start", description="Start frame for restricted range",
        default=1,
    )
    frame_end: IntProperty(
        name="End", description="End frame for restricted range",
        default=250,
    )
    blend_in: FloatProperty(
        name="Blend In",
        description="Ease-in duration (frames) from frame_start",
        default=0.0, min=0.0,
    )
    blend_out: FloatProperty(
        name="Blend Out",
        description="Ease-out duration (frames) ending at frame_end",
        default=0.0, min=0.0,
    )


# ------------------------- Operator -------------------------

class AOD_OT_duplicate(Operator):
    """Duplicate active animated object, offset keyframes per copy,
    and apply Cycles per the panel settings (optionally to the original too)."""
    bl_idname = "object.aod_duplicate_cycles_configurable"
    bl_label = "Create Duplicates"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.aod_settings
        src = context.active_object

        if src is None or not _object_has_action(src):
            self.report({'ERROR'}, "Active object must have an Action with keyframes.")
            return {'CANCELLED'}

        base_action = src.animation_data.action

        # Optionally apply cycles to the original (no time shift).
        if s.apply_to_original:
            _apply_cycles_modifier(base_action, s)

        made = 0
        for i in range(1, s.copies + 1):
            dx = s.frame_offset * i

            new_obj = src.copy()
            new_obj.data = src.data.copy() if src.data else None
            context.collection.objects.link(new_obj)

            new_obj.animation_data_create()
            new_action = base_action.copy()
            new_action.name = f"{base_action.name}_dup_{i:02d}"
            new_obj.animation_data.action = new_action

            if dx:
                _offset_action_keyframes_in_time(new_action, dx)

            _apply_cycles_modifier(new_action, s)
            made += 1

        self.report({'INFO'}, f"Created {made} duplicates.")
        return {'FINISHED'}


# ------------------------- UI Panel -------------------------

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
        col.prop(s, "copies")
        col.prop(s, "frame_offset")

        layout.separator(factor=0.5)
        layout.label(text="Cycles Modifier:")
        row = layout.row(align=True)
        row.prop(s, "mode_before")
        row.prop(s, "mode_after")

        row = layout.row(align=True)
        row.prop(s, "cycles_before")
        row.prop(s, "cycles_after")

        # Apply to original appears just before Use Influence
        layout.prop(s, "apply_to_original")

        layout.prop(s, "use_influence")
        if s.use_influence:
            layout.prop(s, "influence")

        layout.prop(s, "use_restricted_range")
        if s.use_restricted_range:
            row = layout.row(align=True)
            row.prop(s, "frame_start")
            row.prop(s, "frame_end")
            row = layout.row(align=True)
            row.prop(s, "blend_in")
            row.prop(s, "blend_out")

        layout.separator()
        layout.operator("object.aod_duplicate_cycles_configurable", icon='DUPLICATE')


# ------------------------- Registration -------------------------

classes = (AOD_Settings, AOD_OT_duplicate, AOD_PT_panel)

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
