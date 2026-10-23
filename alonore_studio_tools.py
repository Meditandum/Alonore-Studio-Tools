# Alonore Studio Tools — Blender add-on
# Copyright (C) 2026 Alonore Art
# SPDX-License-Identifier: GPL-3.0-or-later
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# This program is distributed WITHOUT ANY WARRANTY; without even the
# implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the LICENSE file for details.

bl_info = {
    'name': 'Alonore Studio Tools', 'author': 'Alonore Art',
    'version': (0, 11, 6), 'blender': (4, 2, 0),
    'location': 'View3D > Sidebar > Alonore',
    'description': 'Studio lighting with adjustable softboxes, camera, presets and creative colors',
    'category': 'Lighting',
}
import bpy
import math
import random
import colorsys
import json
import time
import gpu
import blf
from gpu_extras.batch import batch_for_shader
from pathlib import Path
from mathutils import Vector, Quaternion
from bpy_extras import view3d_utils

COLLECTION_NAME = 'Alonore Studio Setup'
LIGHTS = {
    'key': ('Alonore Key', 450.0, 1.7, -2.0, 2.1, 1.5),
    'fill': ('Alonore Fill', 220.0, -2.0, -1.1, 0.9, 2.0),
    'rim': ('Alonore Rim', 650.0, -0.7, 1.8, 1.7, 1.0),
    'background': ('Alonore Background', 300.0, 0.0, 3.0, 2.0, 2.0),
    'accent': ('Alonore Accent', 250.0, 1.5, 1.6, 1.2, 0.7),
}


def get_collection(scene):
    return next((c for c in scene.collection.children if c.name == COLLECTION_NAME), None)


def get_light(scene, key):
    collection = get_collection(scene)
    if collection is None:
        return None
    return next((o for o in collection.objects if o.get('alonore_role') == key and o.type == 'LIGHT'), None)


def model_objects(context):
    selected = [o for o in context.selected_objects if o.type == 'MESH']
    if len(selected) > 1:
        return selected
    active = context.active_object
    if active and active.type == 'MESH':
        if active.parent:
            family = [o for o in active.parent.children_recursive if o.type == 'MESH']
            if family:
                return family
        # If one mesh from a multi-part product is selected, use its collection.
        for col in active.users_collection:
            if col.name != 'Collection' and col.name != COLLECTION_NAME:
                meshes = [o for o in col.all_objects if o.type == 'MESH']
                if len(meshes) > 1:
                    return meshes
        return [active]
    return []


def combined_bounds(objects):
    pts = [o.matrix_world @ Vector(v) for o in objects for v in o.bound_box]
    if not pts:
        raise ValueError('No mesh objects for framing')
    mins = Vector([min(p[i] for p in pts) for i in range(3)])
    maxs = Vector([max(p[i] for p in pts) for i in range(3)])
    return (mins + maxs) / 2, max(maxs[i] - mins[i] for i in range(3)), pts


def bounds(obj):
    pts = [obj.matrix_world @ Vector(p) for p in obj.bound_box]
    center = sum(pts, Vector()) / 8
    span = max(max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3))
    return center, max(span, 0.25)


def aim(obj, target):
    direction = Vector(target) - obj.location
    if direction.length > 1e-6:
        obj.rotation_euler = direction.to_track_quat('-Z', 'Y').to_euler()


def kelvin_rgb(k):
    t = max(1000.0, min(12000.0, k)) / 100.0
    if t <= 66:
        r = 255
        g = 99.4708025861 * math.log(t) - 161.1195681661
        b = 0 if t <= 19 else 138.5177312231 * math.log(t - 10) - 305.0447927307
    else:
        r = 329.698727446 * (t - 60) ** -0.1332047592
        g = 288.1221695283 * (t - 60) ** -0.0755148492
        b = 255
    return tuple(max(0, min(1, c / 255.0)) for c in (r, g, b))


def creative_rgb(temperature, saturation):
    """Artistic extension of Kelvin color away from neutral white."""
    base = kelvin_rgb(temperature)
    # Amplify the temperature-dependent chroma without changing overall peak.
    # 6500 K is nearly neutral; extreme temperatures become more colorful.
    peak = max(base)
    return tuple(max(0.0, min(1.0, peak + (channel - peak) * saturation)) for channel in base)


def update_color_controls(self, context):
    global _applying
    if _applying:
        return
    if self.color_mode == 'PHYSICAL':
        color = kelvin_rgb(self.temperature)
    elif self.color_mode == 'CREATIVE':
        color = creative_rgb(self.temperature, self.saturation)
    else:
        update_rig(self, context)
        return
    _applying = True
    try:
        self.color = color
    finally:
        _applying = False
    update_rig(self, context)


def update_manual_color(self, context):
    global _applying
    if not _applying:
        _applying = True
        try:
            self.color_mode = 'MANUAL'
        finally:
            _applying = False
        update_rig(self, context)


def refresh(scene):
    collection = get_collection(scene)
    if collection is None or 'alonore_center' not in collection:
        return
    center = Vector(collection['alonore_center'])
    scale = float(collection.get('alonore_scale', 1.0))
    for key in LIGHTS:
        obj = get_light(scene, key)
        if obj is None:
            continue
        settings = getattr(scene, 'alonore_' + key)
        angle = settings.angle
        radius = scale * settings.distance
        obj.location = center + Vector((radius * math.cos(angle), radius * math.sin(angle), scale * settings.height))
        target = Vector(settings.paint_target) if settings.use_paint_target else center
        aim(obj, target)
        # Rotate the emission plane around its local forward axis.
        obj.rotation_mode = 'QUATERNION'
        base = (target - obj.location).to_track_quat('-Z', 'Y')
        obj.rotation_quaternion = base @ Quaternion((0, 0, 1), settings.roll)
        obj.data.energy = settings.power * scale * scale * scene.alonore_light_multiplier
        shape = settings.softbox_shape
        if shape == 'RECTANGLE':
            obj.data.shape = 'RECTANGLE'
            obj.data.size = max(0.01, settings.width * scale)
            obj.data.size_y = max(0.01, settings.height_size * scale)
        elif shape == 'STRIP':
            obj.data.shape = 'RECTANGLE'
            obj.data.size = max(0.01, settings.strip_width * scale)
            obj.data.size_y = max(0.01, settings.strip_height * scale)
        else:
            obj.data.shape = shape
            obj.data.size = max(0.01, settings.size * scale)
        obj.data.spread = settings.spread
        obj.data.color = settings.color[:]


_applying = False

# Persistent viewport markers are real Blender Empty objects. Unlike a modal GPU
# draw handler, they survive leaving LightPaint and saving/reopening the .blend.
MARKER_COLORS = {
    'key': (1.0, 0.42, 0.10, 1.0),
    'fill': (0.12, 0.75, 1.0, 1.0),
    'rim': (0.73, 0.28, 1.0, 1.0),
    'background': (0.3, 1.0, 0.45, 1.0),
    'accent': (1.0, 0.85, 0.15, 1.0),
}


def get_target_marker(scene, key):
    studio = get_collection(scene)
    if studio is None:
        return None
    return next((obj for obj in studio.objects
                 if obj.get('alonore_target_marker') == key), None)


# The Empty objects store targets in .blend; the screen overlay draws them.
# POST_PIXEL is depth-independent, so Always On Top has predictable behavior.
_marker_overlay_handle = None


def draw_saved_markers():
    context = bpy.context
    scene = context.scene
    region = context.region
    space = context.space_data
    if not scene or not region or not space or space.type != 'VIEW_3D':
        return
    if not getattr(scene, 'alonore_paint_marker', False) or not space.overlay.show_overlays:
        return
    studio = get_collection(scene)
    if studio is None:
        return
    rv3d = space.region_3d
    shader = gpu.shader.from_builtin('UNIFORM_COLOR')
    radius = max(5, int(12 + scene.alonore_marker_size * 65))
    for key in LIGHTS:
        settings = getattr(scene, 'alonore_' + key)
        marker = get_target_marker(scene, key)
        if marker is None or not settings.use_paint_target:
            continue
        point = Vector(marker.location)
        projected = view3d_utils.location_3d_to_region_2d(region, rv3d, point)
        if projected is None:
            continue
        if not scene.alonore_marker_in_front:
            # In normal mode hide markers behind scene geometry.
            eye = view3d_utils.region_2d_to_origin_3d(region, rv3d, projected)
            ray = point - eye
            distance = ray.length
            if distance > 1e-5:
                direction = ray / distance
                hit, hit_point, _, _, _, _ = scene.ray_cast(
                    context.evaluated_depsgraph_get(), eye, direction,
                    distance=max(0.0, distance - max(0.001, distance * 0.002)))
                if hit:
                    continue
        x, y = projected
        segments = 40
        vertices = [(x + radius * math.cos(i * math.tau / segments),
                     y + radius * math.sin(i * math.tau / segments)) for i in range(segments + 1)]
        vertices += [(x - radius - 5, y), (x + radius + 5, y),
                     (x, y - radius - 5), (x, y + radius + 5)]
        batch = batch_for_shader(shader, 'LINES', {'pos': vertices},
                                indices=[(i, i + 1) for i in range(segments)] +
                                        [(segments + 1, segments + 2), (segments + 3, segments + 4)])
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', MARKER_COLORS[key])
        batch.draw(shader)
        gpu.state.blend_set('NONE')
        if scene.alonore_marker_labels:
            blf.size(0, 12)
            blf.color(0, *MARKER_COLORS[key])
            blf.position(0, x + radius + 8, y + 4, 0)
            blf.draw(0, marker.name)


def style_target_marker(scene, marker, key):
    """Empty remains as saved target; overlay handles viewport presentation."""
    studio = get_collection(scene)
    if studio is None:
        return
    scale = max(0.001, float(studio.get('alonore_scale', 1.0)))
    marker.empty_display_type = 'SPHERE'
    marker.empty_display_size = max(0.01, scale * scene.alonore_marker_size)
    marker.show_in_front = False
    marker.show_name = False
    marker.color = MARKER_COLORS[key]
    marker.hide_render = True


def sync_target_markers(scene):
    studio = get_collection(scene)
    if studio is None:
        return
    for key in LIGHTS:
        marker = get_target_marker(scene, key)
        if marker is None:
            continue
        settings = getattr(scene, 'alonore_' + key)
        style_target_marker(scene, marker, key)
        marker.hide_set(True)  # Prevent doubled geometry; the overlay is the visual.
        if settings.use_paint_target:
            marker.location = settings.paint_target[:]
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def store_target_marker(scene, key):
    studio = get_collection(scene)
    if studio is None:
        return
    settings = getattr(scene, 'alonore_' + key)
    marker = get_target_marker(scene, key)
    if marker is None:
        marker = bpy.data.objects.new('Alonore Target - ' + key.title(), None)
        marker['alonore_target_marker'] = key
        studio.objects.link(marker)
    style_target_marker(scene, marker, key)
    marker.location = settings.paint_target[:]
    marker.hide_set(True)
    sync_target_markers(scene)


def update_target_visibility(self, context):
    if context.scene:
        sync_target_markers(context.scene)


def update_paint_target(self, context):
    update_rig(self, context)
    if context.scene and not _applying:
        sync_target_markers(context.scene)


def update_rig(self, context):
    if context.scene is not None:
        if not _applying:
            context.scene.alonore_active_preset = 'MODIFIED'
        refresh(context.scene)

def set_active_modified(self, context):
    if not _applying:
        self.alonore_active_preset = 'MODIFIED'
    refresh(self)



class ALONORE_PG_light(bpy.types.PropertyGroup):
    power: bpy.props.FloatProperty(name='Power', default=450, min=0, max=100000, update=update_rig)
    color: bpy.props.FloatVectorProperty(name='Color', subtype='COLOR', size=3, min=0, max=1, default=(1, 1, 1), update=update_manual_color)
    size: bpy.props.FloatProperty(name='Size', default=1.5, min=0.05, max=20, update=update_rig)
    angle: bpy.props.FloatProperty(name='Angle', default=-0.7853981634, min=-math.pi, max=math.pi, subtype='ANGLE', update=update_rig)
    height: bpy.props.FloatProperty(name='Height', default=1.5, min=-10, max=10, update=update_rig)
    distance: bpy.props.FloatProperty(name='Distance', default=2.5, min=0.2, max=20, update=update_rig)
    temperature: bpy.props.FloatProperty(name='Temperature (K)', default=6500, min=1000, max=12000, update=update_color_controls)
    color_mode: bpy.props.EnumProperty(name='Color Mode', items=[('PHYSICAL','Physical Temperature','Subtle Kelvin colors'), ('CREATIVE','Creative Color','Artistic amplified Kelvin colors'), ('MANUAL','Manual RGB','Use manually selected color')], default='PHYSICAL', update=update_color_controls)
    saturation: bpy.props.FloatProperty(name='Color Saturation', default=2.0, min=0.0, max=5.0, update=update_color_controls)
    softbox_shape: bpy.props.EnumProperty(name='Softbox Shape', items=[('DISK','Disk','Round softbox'),('SQUARE','Square','Square softbox'),('RECTANGLE','Rectangle','Adjustable rectangular softbox'),('STRIP','Strip','Tall narrow strip softbox')], default='DISK', update=update_rig)
    width: bpy.props.FloatProperty(name='Width', default=1.0, min=0.05, max=20, update=update_rig)
    height_size: bpy.props.FloatProperty(name='Height', default=2.0, min=0.05, max=20, update=update_rig)
    strip_width: bpy.props.FloatProperty(name='Strip Width', default=0.35, min=0.05, max=20, update=update_rig)
    strip_height: bpy.props.FloatProperty(name='Strip Height', default=2.5, min=0.05, max=20, update=update_rig)
    roll: bpy.props.FloatProperty(name='Roll', default=0, min=-math.pi, max=math.pi, subtype='ANGLE', update=update_rig)
    spread: bpy.props.FloatProperty(name='Spread', default=math.pi, min=math.radians(1), max=math.pi, subtype='ANGLE', update=update_rig)
    lock_softbox: bpy.props.BoolProperty(name='Lock Softbox', default=False, description='Exclude this softbox from shape/size randomization')
    enabled: bpy.props.BoolProperty(name='Enabled', default=True, description='Turn this light on or off', update=lambda self, context: update_light_count(context.scene, context))
    use_paint_target: bpy.props.BoolProperty(name='Use LightPaint Target', default=False, update=update_paint_target)
    paint_target: bpy.props.FloatVectorProperty(name='LightPaint Target', size=3, subtype='XYZ', default=(0.0, 0.0, 0.0), update=update_paint_target)


class ALONORE_OT_create(bpy.types.Operator):
    bl_idname = 'alonore.create_studio'
    bl_label = 'Create Studio Setup'
    bl_description = 'Create a camera and three adjustable studio lights around the selected mesh'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object is not None and context.active_object.type == 'MESH'

    def execute(self, context):
        scene = context.scene
        if get_collection(scene):
            self.report({'WARNING'}, 'Remove the existing Alonore Studio Setup first')
            return {'CANCELLED'}
        center, scale, points = combined_bounds(model_objects(context))
        studio = bpy.data.collections.new(COLLECTION_NAME)
        scene.collection.children.link(studio)
        studio['alonore_center'] = list(center)
        studio['alonore_scale'] = scale
        for key, (name, power, x, y, height, size) in LIGHTS.items():
            data = bpy.data.lights.new(name, 'AREA')
            data.shape = 'DISK'
            obj = bpy.data.objects.new(name, data)
            obj['alonore_role'] = key
            studio.objects.link(obj)
            settings = getattr(scene, 'alonore_' + key)
            settings.power = power
            settings.size = size
            settings.angle = math.atan2(y, x)
            settings.distance = math.hypot(x, y)
            settings.height = height
            settings.temperature = 6500
            settings.saturation = 2.0
            settings.color = kelvin_rgb(6500)
            settings.color_mode = 'PHYSICAL'
            settings.softbox_shape = 'DISK'
            settings.roll = 0
            settings.spread = math.pi
            settings.enabled = True
            settings.use_paint_target = False
        camera_data = bpy.data.cameras.new('Alonore Camera')
        camera = bpy.data.objects.new('Alonore Camera', camera_data)
        studio.objects.link(camera)
        camera.location = center + Vector((scale * 2.4, -scale * 3.8, scale * 1.7))
        aim(camera, center)
        camera_data.type = 'ORTHO'
        # Fit the complete model in camera coordinates, accounting for image aspect.
        camera_data.ortho_scale = scale * 2.6
        context.view_layer.update()
        aspect = 1920 / 1080
        right = camera.matrix_world.to_quaternion() @ Vector((1, 0, 0))
        up = camera.matrix_world.to_quaternion() @ Vector((0, 1, 0))
        half_w = max(abs((pt - center).dot(right)) for pt in points)
        half_h = max(abs((pt - center).dot(up)) for pt in points)
        camera_data.ortho_scale = max(2 * half_h, 2 * half_w / aspect, 0.1) * scene.alonore_camera_padding
        scene.camera = camera
        scene.render.resolution_x = 1920
        scene.render.resolution_y = 1080
        scene.render.resolution_percentage = 100
        scene.alonore_light_multiplier = 1.0
        scene.alonore_light_count = '3'
        refresh(scene)
        scene.alonore_active_preset = 'MODIFIED'
        self.report({'INFO'}, 'Alonore Studio v0.10.4 created')
        return {'FINISHED'}


PRESETS = {
    'SOFT': [((650, (1, 1, 1), 2.8, -45, 2.2, 3.0)), ((350, (1, 1, 1), 3.5, -145, 1.2, 3.2)), ((500, (1, 1, 1), 2.0, 125, 2.0, 2.5))],
    'DRAMATIC': [((900, (1, .88, .74), 1.0, -45, 2.4, 2.4)), ((65, (.55, .7, 1), 2.0, -150, .8, 3.4)), ((950, (1, 1, 1), .8, 130, 2.3, 2.2))],
    'CINEMATIC': [((650, (1, .35, .13), 1.7, -45, 1.8, 2.6)), ((280, (.12, .48, 1), 2.2, -155, 1.1, 3.0)), ((850, (.18, .4, 1), 1.1, 125, 2.0, 2.1))],
    'LUXURY': [((750, (1, .87, .68), 2.4, -55, 2.2, 2.3)), ((240, (1, 1, 1), 3.3, -145, 1.1, 3.3)), ((950, (1, .78, .48), 1.3, 135, 2.0, 2.1))],
}


def read_settings(scene):
    result = {key: {field: (list(getattr(getattr(scene, 'alonore_' + key), field)) if field in ('color', 'paint_target') else getattr(getattr(scene, 'alonore_' + key), field)) for field in ('power', 'color', 'size', 'angle', 'height', 'distance', 'temperature', 'color_mode', 'saturation', 'softbox_shape', 'width', 'height_size', 'strip_width', 'strip_height', 'roll', 'spread', 'lock_softbox', 'enabled', 'use_paint_target', 'paint_target')} for key in LIGHTS}
    result['master'] = scene.alonore_light_multiplier
    result['count'] = scene.alonore_light_count
    return result


def apply_settings(scene, data):
    global _applying
    _applying = True
    try:
        _apply_settings_internal(scene, data)
    finally:
        _applying = False
    refresh(scene)
    update_light_count(scene, type('Ctx', (), {'scene': scene})())
    sync_target_markers(scene)

def _apply_settings_internal(scene, data):
    scene.alonore_light_multiplier = data.get('master', 1.0)
    scene.alonore_light_count = str(data.get('count', '3'))
    for key in LIGHTS:
        settings = getattr(scene, 'alonore_' + key)
        if key in data and 'color_mode' not in data[key]:
            settings.color_mode = 'MANUAL'
        for field in ('power', 'color', 'size', 'angle', 'height', 'distance', 'temperature', 'color_mode', 'saturation', 'softbox_shape', 'width', 'height_size', 'strip_width', 'strip_height', 'roll', 'spread', 'lock_softbox', 'enabled', 'use_paint_target', 'paint_target'):
            if key in data and field in data[key]:
                setattr(settings, field, data[key][field])


class ALONORE_OT_preset(bpy.types.Operator):
    bl_idname = 'alonore.apply_preset'
    bl_label = 'Apply Lighting Preset'
    bl_options = {'REGISTER', 'UNDO'}
    preset: bpy.props.EnumProperty(items=[(k, k.title(), '') for k in PRESETS])

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        global _applying
        _applying = True
        try:
            for key, (power, color, size, angle, height, distance) in zip(('key','fill','rim'), PRESETS[self.preset]):
                settings = getattr(scene, 'alonore_' + key)
                settings.power = power
                settings.temperature = 6500
                settings.color_mode = 'MANUAL'
                settings.color = color
                settings.size = size
                settings.angle = math.radians(angle)
                settings.height = height
                settings.distance = distance
        finally:
            _applying = False
        scene.alonore_active_preset = self.preset
        refresh(scene)
        return {'FINISHED'}


class ALONORE_OT_random_positions(bpy.types.Operator):
    bl_idname = 'alonore.random_positions'
    bl_label = 'Randomize Positions'
    bl_description = 'Randomize light positions while preserving power, colors and sizes'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        global _applying
        rng = random.Random(scene.alonore_random_seed)
        sectors = {'key': (-85, -15), 'fill': (-175, -105), 'rim': (85, 165), 'background': (50, 130), 'accent': (-10, 70)}
        _applying = True
        try:
            for key in LIGHTS:
                obj = get_light(scene, key)
                if obj is None or obj.alonore_lock_position:
                    continue
                settings = getattr(scene, 'alonore_' + key)
                a, b = sectors[key]
                settings.angle = math.radians(rng.uniform(a, b))
                settings.height = rng.uniform(.6, 2.8)
                settings.distance = rng.uniform(1.5, 4.0)
        finally:
            _applying = False
        scene.alonore_active_preset = 'MODIFIED'
        refresh(scene)
        scene.alonore_random_seed += 1
        return {'FINISHED'}


def preset_file():
    # Store presets in Blender's per-user configuration directory.
    root = bpy.utils.user_resource('CONFIG', path='alonore_studio_tools', create=True)
    if not root:
        raise OSError('Blender configuration directory unavailable')
    return Path(root) / 'presets_v04.json'


def load_library():
    path = preset_file()
    if not path.exists():
        return {}
    with path.open('r', encoding='utf-8') as file:
        data = json.load(file)
    if not isinstance(data, dict):
        raise ValueError('Invalid preset library')
    return data


class ALONORE_OT_save_custom(bpy.types.Operator):
    bl_idname = 'alonore.save_custom'
    bl_label = 'Save Preset to Slot'
    bl_description = 'Save lighting settings to a reusable slot (overwrites this slot)'
    bl_options = {'REGISTER'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        try:
            data = load_library()
            slot = str(scene.alonore_preset_slot)
            data[slot] = {'name': scene.alonore_custom_name.strip() or ('Slot ' + slot), 'settings': read_settings(scene)}
            path = preset_file()
            temporary = path.with_suffix('.tmp')
            with temporary.open('w', encoding='utf-8') as file:
                json.dump(data, file, indent=2, ensure_ascii=False)
            temporary.replace(path)
            scene.alonore_active_preset = 'CUSTOM_' + slot
        except (OSError, ValueError, TypeError) as exc:
            self.report({'ERROR'}, 'Cannot save preset: ' + str(exc))
            return {'CANCELLED'}
        self.report({'INFO'}, 'Saved preset to slot ' + slot)
        return {'FINISHED'}


class ALONORE_OT_load_custom(bpy.types.Operator):
    bl_idname = 'alonore.load_custom'
    bl_label = 'Load Preset from Slot'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        try:
            slot = str(scene.alonore_preset_slot)
            data = load_library()
            if slot not in data:
                self.report({'WARNING'}, 'Slot ' + slot + ' is empty')
                return {'CANCELLED'}
            entry = data[slot]
            apply_settings(scene, entry.get('settings', entry))
            scene.alonore_custom_name = entry.get('name', 'Slot ' + slot)
            scene.alonore_active_preset = 'CUSTOM_' + slot
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            self.report({'ERROR'}, 'Cannot load preset: ' + str(exc))
            return {'CANCELLED'}
        return {'FINISHED'}


class ALONORE_OT_remove(bpy.types.Operator):
    bl_idname = 'alonore.remove_studio'
    bl_label = 'Remove Studio Setup'
    bl_description = 'Delete the studio rig collection and its contents'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        studio = get_collection(context.scene)
        if studio is None:
            self.report({'WARNING'}, 'No Alonore Studio Setup found')
            return {'CANCELLED'}
        for obj in list(studio.objects):
            bpy.data.objects.remove(obj, do_unlink=True)
        bpy.data.collections.remove(studio)
        self.report({'INFO'}, 'Alonore Studio removed')
        return {'FINISHED'}


class ALONORE_OT_lightpaint_reflection(bpy.types.Operator):
    """Interactive reflection placement with optional live preview."""
    bl_idname = 'alonore.lightpaint_reflection'
    bl_label = 'LightPaint Reflection'
    bl_description = 'Click to place; optional live preview, mouse wheel distance, Esc to exit'
    bl_options = {'REGISTER', 'UNDO', 'BLOCKING'}

    @classmethod
    def poll(cls, context):
        return context.area is not None and context.area.type == 'VIEW_3D' and get_collection(context.scene) is not None

    def invoke(self, context, event):
        scene = context.scene
        self.key = scene.alonore_paint_light
        light = get_light(scene, self.key)
        if light is None or not getattr(scene, 'alonore_' + self.key).enabled or light.hide_render:
            self.report({'WARNING'}, 'Selected softbox is not enabled')
            return {'CANCELLED'}
        self.area = context.area
        self.window_region = next((r for r in self.area.regions if r.type == 'WINDOW'), None)
        if self.window_region is None:
            self.report({'WARNING'}, '3D Viewport region unavailable')
            return {'CANCELLED'}
        settings = getattr(scene, 'alonore_' + self.key)
        self.committed = self.snapshot(settings)
        self.preview_active = False
        self.marker = Vector(settings.paint_target) if settings.use_paint_target else None
        self.last_mouse = None
        self.last_update = 0.0
        self.draw_handle = None
        if scene.alonore_paint_marker:
            self.draw_handle = bpy.types.SpaceView3D.draw_handler_add(self.draw_marker, (), 'WINDOW', 'POST_PIXEL')
        context.window_manager.modal_handler_add(self)
        self.area.header_text_set('LightPaint: move to preview | Left: save point | Right: save and exit | Esc: cancel preview | Wheel: distance')
        return {'RUNNING_MODAL'}

    @staticmethod
    def snapshot(settings):
        return (settings.angle, settings.distance, settings.height,
                tuple(settings.paint_target), settings.use_paint_target)

    def restore(self, scene, state):
        global _applying
        settings = getattr(scene, 'alonore_' + self.key)
        _applying = True
        try:
            settings.angle, settings.distance, settings.height = state[:3]
            settings.paint_target = state[3]
            settings.use_paint_target = state[4]
        finally:
            _applying = False
        refresh(scene)

    def finish(self, scene, accept_preview=False):
        if self.preview_active:
            if accept_preview:
                self.committed = self.snapshot(getattr(scene, 'alonore_' + self.key))
                scene.alonore_active_preset = 'MODIFIED'
                store_target_marker(scene, self.key)
            else:
                self.restore(scene, self.committed)
        elif getattr(scene, 'alonore_' + self.key).use_paint_target:
            store_target_marker(scene, self.key)
        if self.draw_handle is not None:
            bpy.types.SpaceView3D.draw_handler_remove(self.draw_handle, 'WINDOW')
            self.draw_handle = None
        if self.area is not None:
            self.area.header_text_set(None)
            self.area.tag_redraw()

    def draw_marker(self):
        if self.marker is None or self.area is None:
            return
        region = self.window_region
        rv3d = self.area.spaces.active.region_3d
        pos = view3d_utils.location_3d_to_region_2d(region, rv3d, self.marker)
        if pos is None:
            return
        x, y = pos
        shader = gpu.shader.from_builtin('UNIFORM_COLOR')
        vertices = [(x-9,y),(x+9,y),(x,y-9),(x,y+9)]
        batch = batch_for_shader(shader, 'LINES', {'pos': vertices})
        gpu.state.blend_set('ALPHA')
        shader.bind()
        shader.uniform_float('color', (1.0, 0.75, 0.1, 0.95))
        batch.draw(shader)
        gpu.state.blend_set('NONE')

    def place(self, context, mouse, commit):
        region = self.window_region
        x, y = mouse
        if not (0 <= x < region.width and 0 <= y < region.height):
            return False
        rv3d = self.area.spaces.active.region_3d
        origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, (x, y))
        direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, (x, y)).normalized()
        depsgraph = context.evaluated_depsgraph_get()
        hit, point, normal, _, obj, _ = context.scene.ray_cast(depsgraph, origin, direction)
        studio = get_collection(context.scene)
        if not hit or obj is None or obj.type != 'MESH' or studio is None:
            return False
        if obj.name in studio.objects:
            return False
        normal = Vector(normal).normalized()
        to_eye = (origin - point).normalized()
        if normal.dot(to_eye) < 0:
            normal.negate()
        reflection = 2.0 * normal.dot(to_eye) * normal - to_eye
        if reflection.length_squared < 1e-8:
            return False
        reflection.normalize()
        scale = max(0.001, float(studio.get('alonore_scale', 1.0)))
        center = Vector(studio['alonore_center'])
        location = Vector(point) + reflection * context.scene.alonore_paint_distance * scale
        relative = (location - center) / scale
        settings = getattr(context.scene, 'alonore_' + self.key)
        global _applying
        _applying = True
        try:
            settings.angle = math.atan2(relative.y, relative.x)
            settings.distance = max(0.2, min(20.0, math.hypot(relative.x, relative.y)))
            settings.height = max(-10.0, min(10.0, relative.z))
            settings.paint_target = tuple(point)
            settings.use_paint_target = True
        finally:
            _applying = False
        refresh(context.scene)
        self.marker = Vector(point)
        self.preview_active = not commit
        if commit:
            self.committed = self.snapshot(settings)
            store_target_marker(context.scene, self.key)
            context.scene.alonore_active_preset = 'MODIFIED'
        self.area.tag_redraw()
        return True

    def modal(self, context, event):
        if event.type in {'ESC', 'RIGHTMOUSE'} and event.value == 'PRESS':
            self.finish(context.scene, accept_preview=(event.type == 'RIGHTMOUSE'))
            return {'FINISHED'}
        if context.area != self.area:
            return {'RUNNING_MODAL'}
        mouse = (event.mouse_x - self.area.x - self.window_region.x,
                 event.mouse_y - self.area.y - self.window_region.y)
        if event.type in {'WHEELUPMOUSE', 'WHEELDOWNMOUSE'} and event.value == 'PRESS':
            factor = 1.1 if event.type == 'WHEELUPMOUSE' else 1.0/1.1
            scene = context.scene
            scene.alonore_paint_distance = max(0.2, min(20.0, scene.alonore_paint_distance * factor))
            if self.last_mouse is not None:
                self.place(context, self.last_mouse, commit=not scene.alonore_paint_live)
                if not scene.alonore_paint_live:
                    self.committed = self.snapshot(getattr(scene, 'alonore_' + self.key))
            return {'RUNNING_MODAL'}
        if event.type == 'MOUSEMOVE':
            self.last_mouse = mouse
            if context.scene.alonore_paint_live:
                now = time.monotonic()
                if now - self.last_update >= 0.10:
                    self.last_update = now
                    if not self.place(context, mouse, commit=False) and self.preview_active:
                        self.restore(context.scene, self.committed)
                        self.preview_active = False
                        self.marker = None
                        self.area.tag_redraw()
            return {'RUNNING_MODAL'}
        if event.type == 'LEFTMOUSE' and event.value == 'PRESS':
            self.last_mouse = mouse
            if not self.place(context, mouse, commit=True):
                self.report({'WARNING'}, 'Click a mesh surface inside the 3D viewport')
            return {'RUNNING_MODAL'}
        return {'RUNNING_MODAL'}


# v0.11: additional user-managed lights. Existing five legacy lights remain
# untouched, preserving old .blend rigs, LightPaint and legacy presets.
DYNAMIC_TYPES = [('AREA', 'Area', ''), ('POINT', 'Point', ''),
                 ('SPOT', 'Spot', ''), ('SUN', 'Sun', '')]


def dynamic_lights(scene):
    studio = get_collection(scene)
    return sorted((obj for obj in studio.objects if obj.type == 'LIGHT' and obj.get('alonore_dynamic_light')), key=lambda o: o.name) if studio else []


def selected_dynamic(scene):
    items = dynamic_lights(scene)
    idx = scene.alonore_dynamic_index
    return items[idx] if 0 <= idx < len(items) else None


class ALONORE_PG_dynamic_entry(bpy.types.PropertyGroup):
    obj: bpy.props.PointerProperty(type=bpy.types.Object)


class ALONORE_UL_dynamic_lights(bpy.types.UIList):
    """Selectable, scrollable list of additional lights."""
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        obj = item.obj
        if obj is None or obj.type != 'LIGHT' or not obj.get('alonore_dynamic_light'):
            return
        row = layout.row(align=True)
        icon_name = {'AREA': 'LIGHT_AREA', 'POINT': 'LIGHT_POINT',
                     'SPOT': 'LIGHT_SPOT', 'SUN': 'LIGHT_SUN'}.get(obj.data.type, 'LIGHT')
        row.prop(obj, 'name', text='', emboss=False, icon=icon_name)
        row.label(text=obj.data.type.title())
        row.prop(obj, 'hide_render', text='', icon='HIDE_ON' if obj.hide_render else 'HIDE_OFF', emboss=False)


def unified_lights(scene):
    """Read-only view of legacy and user lights; safe to call from Panel.draw."""
    original = [(key, get_light(scene, key)) for key in LIGHTS]
    return [(key, obj) for key, obj in original if obj is not None] + [('', obj) for obj in dynamic_lights(scene)]


def selected_unified(scene):
    name = scene.alonore_unified_selected
    return next(((key, obj) for key, obj in unified_lights(scene) if obj.name == name), (None, None))


class ALONORE_OT_unified_select(bpy.types.Operator):
    bl_idname = 'alonore.unified_select'
    bl_label = 'Select Studio Light'
    bl_options = {'INTERNAL'}
    light_name: bpy.props.StringProperty()

    def execute(self, context):
        scene = context.scene
        obj = next((o for _, o in unified_lights(scene) if o.name == self.light_name), None)
        if obj is None:
            return {'CANCELLED'}
        scene.alonore_unified_selected = obj.name
        if obj.get('alonore_dynamic_light'):
            scene.alonore_dynamic_index = dynamic_lights(scene).index(obj)
        if obj.get('alonore_role') in LIGHTS:
            scene.alonore_paint_light = obj['alonore_role']
        for old in context.selected_objects:
            old.select_set(False)
        obj.hide_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        return {'FINISHED'}


class ALONORE_OT_unified_from_viewport(bpy.types.Operator):
    bl_idname = 'alonore.unified_from_viewport'
    bl_label = 'Use Viewport Selection'
    bl_description = 'Make the selected Blender light active in Unified Light Manager'
    bl_options = {'INTERNAL'}

    def execute(self, context):
        obj = context.active_object
        if obj and any(obj == candidate for _, candidate in unified_lights(context.scene)):
            context.scene.alonore_unified_selected = obj.name
            if obj.get('alonore_dynamic_light'):
                context.scene.alonore_dynamic_index = dynamic_lights(context.scene).index(obj)
            return {'FINISHED'}
        self.report({'WARNING'}, 'Select a light from the Alonore studio collection')
        return {'CANCELLED'}


class ALONORE_OT_dynamic_select(bpy.types.Operator):
    bl_idname = 'alonore.dynamic_select'
    bl_label = 'Select Additional Light'
    bl_options = {'INTERNAL'}
    index: bpy.props.IntProperty(default=0)

    def execute(self, context):
        lights = dynamic_lights(context.scene)
        if 0 <= self.index < len(lights):
            context.scene.alonore_dynamic_index = self.index
            return {'FINISHED'}
        return {'CANCELLED'}


# The baseline is saved on each light object, not on the Scene. This makes it
# survive .blend saves and prevents cumulative dimming on repeated randomization.
POWER_BASE_KEY = 'alonore_power_base_v114'


def power_targets(scene):
    scope = scene.alonore_power_scope
    if scope in {'LEGACY', 'ALL'}:
        for key in list(LIGHTS)[:int(scene.alonore_light_count)]:
            obj = get_light(scene, key)
            settings = getattr(scene, 'alonore_' + key)
            if obj is not None and settings.enabled and not obj.hide_render and not obj.alonore_lock_power:
                yield obj, settings
    if scope in {'DYNAMIC', 'ALL'}:
        for obj in dynamic_lights(scene):
            if not obj.hide_render and not obj.alonore_lock_power:
                yield obj, None


class ALONORE_OT_capture_power_base(bpy.types.Operator):
    bl_idname = 'alonore.capture_power_base'
    bl_label = 'Set Current as Base'
    bl_description = 'Save current powers as the reference for future power randomization'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            return {'CANCELLED'}
        count = 0
        for obj, settings in power_targets(scene):
            obj[POWER_BASE_KEY] = float(settings.power if settings else obj.data.energy)
            count += 1
        self.report({'INFO'}, 'Captured base power for %d lights' % count)
        return {'FINISHED'}


class ALONORE_OT_random_power(bpy.types.Operator):
    bl_idname = 'alonore.random_power'
    bl_label = 'Randomize Light Power'
    bl_description = 'Randomize each light around its saved base power, without cumulative dimming'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        lower = scene.alonore_power_min / 100.0
        upper = scene.alonore_power_max / 100.0
        if lower > upper:
            self.report({'ERROR'}, 'Minimum percentage must not exceed maximum')
            return {'CANCELLED'}
        rng = random.Random(scene.alonore_power_seed)
        global _applying
        count = 0
        _applying = True
        try:
            for obj, settings in power_targets(scene):
                if POWER_BASE_KEY not in obj:
                    obj[POWER_BASE_KEY] = float(settings.power if settings else obj.data.energy)
                base = max(0.0, float(obj[POWER_BASE_KEY]))
                power = base * rng.uniform(lower, upper)
                if settings:
                    settings.power = max(0.0, min(100000.0, power))
                else:
                    obj.data.energy = max(0.0, power)
                count += 1
        finally:
            _applying = False
        refresh(scene)
        scene.alonore_power_seed += 1
        scene.alonore_active_preset = 'MODIFIED'
        self.report({'INFO'}, 'Randomized %d lights relative to saved base power' % count)
        return {'FINISHED'}


class ALONORE_OT_dynamic_type(bpy.types.Operator):
    bl_idname = 'alonore.dynamic_type'
    bl_label = 'Change Light Type'
    bl_options = {'REGISTER', 'UNDO'}
    kind: bpy.props.EnumProperty(items=DYNAMIC_TYPES)

    def execute(self, context):
        obj = selected_dynamic(context.scene)
        if obj is None:
            return {'CANCELLED'}
        if obj.data.type == self.kind:
            return {'FINISHED'}
        old = obj.data
        data = bpy.data.lights.new(obj.name + ' Data', self.kind)
        data.energy = old.energy
        data.color = old.color[:]
        obj.data = data
        if old.users == 0:
            bpy.data.lights.remove(old)
        context.scene.alonore_active_preset = 'MODIFIED'
        return {'FINISHED'}


class ALONORE_OT_dynamic_add(bpy.types.Operator):
    bl_idname = 'alonore.dynamic_add'
    bl_label = 'Add Studio Light'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        studio = get_collection(context.scene)
        if studio is None:
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        scene = context.scene
        kind = scene.alonore_new_light_type
        index = len(dynamic_lights(scene)) + 1
        name = 'Alonore Custom %02d' % index
        data = bpy.data.lights.new(name, kind)
        data.energy = 450 if kind != 'SUN' else 2
        if kind == 'AREA':
            data.shape = 'DISK'
            data.size = max(0.1, float(studio.get('alonore_scale', 1.0)))
        obj = bpy.data.objects.new(name, data)
        obj['alonore_dynamic_light'] = True
        studio.objects.link(obj)
        center = Vector(studio.get('alonore_center', (0, 0, 0)))
        scale = max(0.1, float(studio.get('alonore_scale', 1.0)))
        obj.location = center + Vector((2 * scale, -2 * scale, 2 * scale))
        aim(obj, center)
        scene.alonore_dynamic_index = dynamic_lights(scene).index(obj)
        scene.alonore_unified_selected = obj.name
        scene.alonore_active_preset = 'MODIFIED'
        return {'FINISHED'}


class ALONORE_OT_dynamic_remove(bpy.types.Operator):
    bl_idname = 'alonore.dynamic_remove'
    bl_label = 'Remove Selected Light'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        obj = selected_dynamic(scene)
        if obj is None:
            return {'CANCELLED'}
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data.users == 0:
            bpy.data.lights.remove(data)
        scene.alonore_dynamic_index = max(0, min(scene.alonore_dynamic_index, len(dynamic_lights(scene)) - 1))
        scene.alonore_unified_selected = ''
        scene.alonore_active_preset = 'MODIFIED'
        return {'FINISHED'}


class ALONORE_OT_dynamic_duplicate(bpy.types.Operator):
    bl_idname = 'alonore.dynamic_duplicate'
    bl_label = 'Duplicate Selected Light'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        obj = selected_dynamic(scene)
        if obj is None:
            return {'CANCELLED'}
        copy = obj.copy()
        copy.data = obj.data.copy()
        get_collection(scene).objects.link(copy)
        copy.location.x += max(0.1, float(get_collection(scene).get('alonore_scale', 1.0)) * .35)
        scene.alonore_dynamic_index = dynamic_lights(scene).index(copy)
        scene.alonore_unified_selected = copy.name
        scene.alonore_active_preset = 'MODIFIED'
        return {'FINISHED'}


class ALONORE_PT_studio(bpy.types.Panel):
    bl_label = 'Alonore Studio Tools'
    bl_idname = 'ALONORE_PT_studio'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Alonore'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.label(text='STUDIO LIGHTING', icon='LIGHT_AREA')
        layout.operator('alonore.create_studio', icon='ADD')
        layout.prop(scene, 'alonore_light_multiplier', text='Master Energy', slider=True)
        box = layout.box()
        box.label(text='LIGHTING PRESETS', icon='PRESET')
        grid = box.grid_flow(columns=2, align=True)
        for name in PRESETS:
            op = grid.operator('alonore.apply_preset', text=name.title(), depress=scene.alonore_active_preset == name)
            op.preset = name
        active = scene.alonore_active_preset
        label = ('Custom Slot ' + active.split('_')[-1]) if active.startswith('CUSTOM_') else (active.title() if active != 'MODIFIED' else 'Custom / Modified')
        box.label(text='Active: ' + label, icon='CHECKMARK' if active != 'MODIFIED' else 'GREASEPENCIL')
        box = layout.box()
        box.label(text='CUSTOM PRESET LIBRARY', icon='FILE_FOLDER')
        box.prop(scene, 'alonore_preset_slot', text='Slot')
        box.prop(scene, 'alonore_custom_name', text='Name')
        row = box.row(align=True)
        row.operator('alonore.save_custom', text='Save Slot', icon='FILE_TICK')
        row.operator('alonore.load_custom', text='Load Slot', icon='IMPORT')
        box = layout.box()
        box.label(text='LIGHT RIG', icon='LIGHT')
        box.prop(scene, 'alonore_light_count', text='Lights', expand=True)
        box.prop(scene, 'alonore_camera_padding', text='Camera Padding')
        box = layout.box()
        box.label(text='UNIFIED LIGHT MANAGER (v0.11.6)', icon='LIGHT')
        box.prop(scene, 'alonore_new_light_type', text='New Type')
        row = box.row(align=True)
        row.operator('alonore.dynamic_add', text='Add Light', icon='ADD')
        row.operator('alonore.unified_from_viewport', text='', icon='RESTRICT_SELECT_OFF')
        all_lights = unified_lights(scene)
        box.label(text='Studio lights: %d' % len(all_lights))
        for key, light in all_lights:
            row = box.row(align=True)
            light_icon = {'AREA': 'LIGHT_AREA', 'POINT': 'LIGHT_POINT',
                          'SPOT': 'LIGHT_SPOT', 'SUN': 'LIGHT_SUN'}.get(light.data.type, 'LIGHT')
            op = row.operator('alonore.unified_select', text=light.name,
                              icon=light_icon, depress=scene.alonore_unified_selected == light.name)
            op.light_name = light.name
            row.prop(light, 'hide_render', text='', icon='HIDE_ON' if light.hide_render else 'HIDE_OFF')
        key, obj = selected_unified(scene)
        if obj is not None:
            details = box.box()
            details.label(text='SELECTED LIGHT', icon='PROPERTIES')
            details.prop(obj, 'name', text='Name')
            locks = details.box()
            locks.label(text='RANDOMIZER LOCKS', icon='LOCKED')
            lock_row = locks.row(align=True)
            lock_row.prop(obj, 'alonore_lock_position', text='Position', toggle=True)
            lock_row.prop(obj, 'alonore_lock_power', text='Power', toggle=True)
            lock_row.prop(obj, 'alonore_lock_color', text='Color', toggle=True)
            if key:
                settings = getattr(scene, 'alonore_' + key)
                details.label(text='Original studio light: ' + key.title())
                details.prop(settings, 'enabled')
                details.prop(settings, 'power')
                details.prop(settings, 'color_mode')
                if settings.color_mode != 'MANUAL':
                    details.prop(settings, 'temperature')
                if settings.color_mode == 'CREATIVE':
                    details.prop(settings, 'saturation')
                details.prop(settings, 'color')
                details.prop(settings, 'softbox_shape')
                if settings.softbox_shape == 'RECTANGLE':
                    details.prop(settings, 'width')
                    details.prop(settings, 'height_size')
                elif settings.softbox_shape == 'STRIP':
                    details.prop(settings, 'strip_width')
                    details.prop(settings, 'strip_height')
                else:
                    details.prop(settings, 'size')
                    details.label(text=' ')  # Keep panel height stable on All Shapes.
                details.prop(settings, 'roll')
                details.prop(settings, 'spread')
                details.prop(settings, 'lock_softbox')
                details.prop(settings, 'angle')
                details.prop(settings, 'height')
                details.prop(settings, 'distance')
                details.label(text='Core lights use legacy rig controls')
            else:
                row = details.row(align=True)
                row.operator('alonore.dynamic_duplicate', text='Duplicate', icon='DUPLICATE')
                row.operator('alonore.dynamic_remove', text='Remove', icon='REMOVE')
                row = details.row(align=True)
                for kind, title, _ in DYNAMIC_TYPES:
                    op = row.operator('alonore.dynamic_type', text=title, depress=obj.data.type == kind)
                    op.kind = kind
                details.prop(obj.data, 'energy', text='Power')
                details.prop(obj.data, 'color', text='Color')
                if obj.data.type == 'AREA':
                    details.prop(obj.data, 'shape')
                    details.prop(obj.data, 'size')
                    if obj.data.shape in {'RECTANGLE', 'ELLIPSE'}:
                        details.prop(obj.data, 'size_y')
                    details.prop(obj.data, 'spread')
                elif obj.data.type == 'SPOT':
                    details.prop(obj.data, 'spot_size')
                    details.prop(obj.data, 'spot_blend')
                details.prop(obj, 'location')
                details.prop(obj, 'rotation_euler', text='Rotation')
        box.label(text='Core light editing is compatible with old rigs')
        power_box = layout.box()
        power_box.label(text='POWER RANDOMIZER', icon='LIGHT')
        power_box.prop(scene, 'alonore_power_scope', text='Lights')
        power_box.prop(scene, 'alonore_power_seed', text='Seed')
        row = power_box.row(align=True)
        row.prop(scene, 'alonore_power_min', text='Min %')
        row.prop(scene, 'alonore_power_max', text='Max %')
        power_box.operator('alonore.random_power', text='Randomize Power', icon='FILE_REFRESH')
        power_box.operator('alonore.capture_power_base', text='Set Current as Base', icon='IMPORT')
        power_box.label(text='Percent of saved base, not previous result')
        box = layout.box()
        box.label(text='POSITION RANDOMIZER', icon='RNDCURVE')
        box.prop(scene, 'alonore_random_seed', text='Seed')
        box.operator('alonore.random_positions', icon='FILE_REFRESH')
        box = layout.box()
        box.label(text='COLOR TEMPERATURE', icon='COLOR')
        box.prop(scene, 'alonore_random_seed', text='Seed')
        box.prop(scene, 'alonore_temperature_mode', text='Mode')
        box.operator('alonore.random_temperature', icon='FILE_REFRESH')
        box = layout.box()
        box.label(text='CREATIVE COLOR', icon='COLOR')
        box.prop(scene, 'alonore_creative_palette', text='Palette')
        box.operator('alonore.random_creative', icon='FILE_REFRESH')
        box = layout.box()
        box.label(text='SOFTBOX RANDOMIZER', icon='LIGHT_AREA')
        box.prop(scene, 'alonore_softbox_seed', text='Seed')
        box.prop(scene, 'alonore_softbox_random_mode', text='Mode')
        box.operator('alonore.random_softboxes', icon='FILE_REFRESH')
        box = layout.box()
        box.label(text='LIGHTPAINT - REFLECTION', icon='LIGHT_AREA')
        box.prop(scene, 'alonore_paint_light', text='Softbox')
        box.prop(scene, 'alonore_paint_distance', text='Distance')
        box.prop(scene, 'alonore_paint_live', text='Live Preview')
        box.prop(scene, 'alonore_paint_marker', text='Show Saved Target Markers')
        if scene.alonore_paint_marker:
            box.prop(scene, 'alonore_marker_size', text='Marker Size', slider=True)
            box.prop(scene, 'alonore_marker_in_front', text='Always On Top')
            box.prop(scene, 'alonore_marker_labels', text='Show Marker Names')
        box.operator('alonore.lightpaint_reflection', text='Paint Reflection in Viewport', icon='EYEDROPPER')
        box.label(text='Click to place; wheel adjusts distance')
        box.label(text='Right Click: save and finish; Esc: cancel')
        if scene.alonore_show_legacy_details:
            layout.label(text='LEGACY LIGHT CONTROLS', icon='LIGHT_AREA')
        for key, title in ([('key', 'KEY LIGHT'), ('fill', 'FILL LIGHT'), ('rim', 'RIM LIGHT'), ('background', 'BACKGROUND LIGHT'), ('accent', 'ACCENT LIGHT')][:int(scene.alonore_light_count)] if scene.alonore_show_legacy_details else []):
            settings = getattr(scene, 'alonore_' + key)
            box = layout.box()
            header = box.row(align=True)
            header.label(text=title, icon='LIGHT_AREA')
            header.prop(settings, 'enabled', text='', icon='HIDE_OFF' if settings.enabled else 'HIDE_ON', toggle=True)
            column = box.column(align=True)
            column.enabled = get_light(scene, key) is not None and settings.enabled
            column.prop(settings, 'power')
            column.prop(settings, 'color_mode', text='Color Mode')
            if settings.color_mode != 'MANUAL':
                column.prop(settings, 'temperature')
            if settings.color_mode == 'CREATIVE':
                column.prop(settings, 'saturation', slider=True)
            column.prop(settings, 'color')
            softbox = box.column(align=True)
            softbox.enabled = column.enabled
            softbox.label(text='SOFTBOX', icon='LIGHT_AREA')
            softbox.prop(settings, 'softbox_shape', text='Shape')
            if settings.softbox_shape == 'RECTANGLE':
                softbox.prop(settings, 'width')
                softbox.prop(settings, 'height_size', text='Height')
            elif settings.softbox_shape == 'STRIP':
                softbox.prop(settings, 'strip_width')
                softbox.prop(settings, 'strip_height')
            else:
                softbox.prop(settings, 'size')
            softbox.prop(settings, 'roll')
            softbox.prop(settings, 'spread')
            softbox.prop(settings, 'lock_softbox', icon='LOCKED' if settings.lock_softbox else 'UNLOCKED')
            column.separator()
            column.prop(settings, 'angle')
            column.prop(settings, 'height')
            column.prop(settings, 'distance')
            if settings.use_paint_target:
                column.prop(settings, 'use_paint_target', text='Keep LightPaint Target')
        layout.prop(scene, 'alonore_show_legacy_details', text='Show Legacy Light Panels')
        layout.separator()
        layout.operator('alonore.remove_studio', icon='TRASH')




def update_light_count(self, context):
    if context.scene is None:
        return
    count = int(context.scene.alonore_light_count)
    for index, key in enumerate(LIGHTS):
        light = get_light(context.scene, key)
        if light:
            settings = getattr(context.scene, 'alonore_' + key)
            hidden = index >= count or not settings.enabled
            light.hide_render = hidden
            light.hide_viewport = hidden
    if not _applying:
        context.scene.alonore_active_preset = 'MODIFIED'


# Temperature palettes favor recognizable warm/cool contrasts instead of
# uniformly sampling Kelvin values (which often look nearly white).
TEMPERATURE_PALETTES = {
    'WARM_COOL': (3200, 8000, 11000, 7000, 2800),
    'COOL_WARM': (10000, 4200, 2700, 8500, 3200),
    'ICE_BLUE': (10500, 12000, 8500, 11500, 6500),
    'GOLDEN': (3000, 4200, 2400, 3500, 2700),
    'NEUTRAL': (5500, 6500, 5000, 6000, 5600),
}

class ALONORE_OT_random_temperature(bpy.types.Operator):
    bl_idname = 'alonore.random_temperature'
    bl_label = 'Randomize Temperatures'
    bl_description = 'Apply a warm/cool Kelvin palette to enabled lights without changing their positions'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        rng = random.Random(scene.alonore_random_seed)
        mode = scene.alonore_temperature_mode
        if mode == 'RANDOM':
            mode = rng.choice(tuple(TEMPERATURE_PALETTES))
        temperatures = TEMPERATURE_PALETTES[mode]
        global _applying
        _applying = True
        try:
            for i, key in enumerate(list(LIGHTS)[:int(scene.alonore_light_count)]):
                obj = get_light(scene, key)
                if obj is None or obj.alonore_lock_color:
                    continue
                temperature = max(1000, min(12000, temperatures[i] + rng.uniform(-350, 350)))
                settings = getattr(scene, 'alonore_' + key)
                settings.color_mode = 'PHYSICAL'
                settings.temperature = temperature
                settings.color = kelvin_rgb(temperature)
        finally:
            _applying = False
        refresh(scene)
        scene.alonore_random_seed += 1
        scene.alonore_active_preset = 'MODIFIED'
        self.report({'INFO'}, 'Temperature palette: ' + mode.replace('_', ' ').title())
        return {'FINISHED'}

CREATIVE_PALETTES = {
    'TEAL_ORANGE': ((0.02, .60, 1.0), (1.0, .25, .05), (0.0, .75, .85), (.08, .20, .8), (1.0, .5, .1)),
    'BLUE_AMBER': ((.03, .20, 1.0), (1.0, .52, .12), (.04, .48, 1.0), (.1, .12, .6), (1.0, .25, .06)),
    'MAGENTA_CYAN': ((1.0, .05, .55), (.03, .8, 1.0), (.3, .06, 1.0), (.08, .55, .8), (1.0, .25, .65)),
    'ICE_NEON': ((.02, .35, 1.0), (.05, .8, 1.0), (.22, .12, 1.0), (.04, .3, .85), (.25, .65, 1.0)),
}


class ALONORE_OT_random_creative(bpy.types.Operator):
    bl_idname = 'alonore.random_creative'
    bl_label = 'Randomize Creative Colors'
    bl_description = 'Apply a saturated artistic color palette without moving the lights'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        rng = random.Random(scene.alonore_random_seed)
        mode = scene.alonore_creative_palette
        if mode == 'RANDOM':
            mode = rng.choice(tuple(CREATIVE_PALETTES))
        palette = CREATIVE_PALETTES[mode]
        global _applying
        _applying = True
        try:
            for key, base_color in zip(list(LIGHTS)[:int(scene.alonore_light_count)], palette):
                obj = get_light(scene, key)
                if obj is None or obj.alonore_lock_color:
                    continue
                # Every seed produces a new variation, even for a named palette.
                # Preserve the palette's color identity with modest HSV shifts.
                h, s, v = colorsys.rgb_to_hsv(*base_color)
                h = (h + rng.uniform(-0.035, 0.035)) % 1.0
                s = max(0.0, min(1.0, s * rng.uniform(0.88, 1.12)))
                v = max(0.0, min(1.0, v * rng.uniform(0.85, 1.0)))
                color = colorsys.hsv_to_rgb(h, s, v)
                settings = getattr(scene, 'alonore_' + key)
                settings.color_mode = 'MANUAL'
                settings.color = color
        finally:
            _applying = False
        scene.alonore_active_preset = 'MODIFIED'
        refresh(scene)
        scene.alonore_random_seed += 1
        self.report({'INFO'}, 'Creative palette: ' + mode.replace('_', ' ').title())
        return {'FINISHED'}



class ALONORE_OT_random_softboxes(bpy.types.Operator):
    bl_idname = 'alonore.random_softboxes'
    bl_label = 'Randomize Softboxes'
    bl_description = 'Randomize softbox shapes and dimensions on unlocked active lights; preserve positions, colors and energy'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        scene = context.scene
        if not get_collection(scene):
            self.report({'WARNING'}, 'Create Studio Setup first')
            return {'CANCELLED'}
        rng = random.Random(scene.alonore_softbox_seed)
        global _applying
        _applying = True
        try:
            for key in list(LIGHTS)[:int(scene.alonore_light_count)]:
                settings = getattr(scene, 'alonore_' + key)
                if settings.lock_softbox:
                    continue
                mode = scene.alonore_softbox_random_mode
                if mode == 'ROUND':
                    settings.softbox_shape = 'DISK'
                elif mode == 'ALL':
                    settings.softbox_shape = rng.choice(('DISK', 'SQUARE', 'RECTANGLE', 'STRIP'))
                elif mode == 'STRIPS':
                    settings.softbox_shape = rng.choice(('RECTANGLE', 'STRIP'))
                if settings.softbox_shape in {'DISK', 'SQUARE'}:
                    settings.size = rng.uniform(0.6, 3.4)
                elif settings.softbox_shape == 'RECTANGLE':
                    settings.width = rng.uniform(0.35, 2.2)
                    settings.height_size = rng.uniform(0.7, 3.6)
                else:
                    settings.strip_width = rng.uniform(0.15, 0.65)
                    settings.strip_height = rng.uniform(1.4, 4.0)
                settings.roll = math.radians(rng.uniform(-80, 80))
                settings.spread = math.radians(rng.uniform(80, 180))
        finally:
            _applying = False
        refresh(scene)
        scene.alonore_active_preset = 'MODIFIED'
        scene.alonore_softbox_seed += 1
        return {'FINISHED'}

CLASSES = (ALONORE_OT_unified_select, ALONORE_OT_unified_from_viewport, ALONORE_OT_dynamic_select, ALONORE_PG_dynamic_entry, ALONORE_UL_dynamic_lights, ALONORE_OT_capture_power_base, ALONORE_OT_random_power, ALONORE_OT_dynamic_type, ALONORE_OT_dynamic_add, ALONORE_OT_dynamic_remove, ALONORE_OT_dynamic_duplicate, ALONORE_PG_light, ALONORE_OT_create, ALONORE_OT_preset, ALONORE_OT_random_positions, ALONORE_OT_random_temperature, ALONORE_OT_random_creative, ALONORE_OT_random_softboxes, ALONORE_OT_lightpaint_reflection, ALONORE_OT_save_custom, ALONORE_OT_load_custom, ALONORE_OT_remove, ALONORE_PT_studio)


def register():
    global _marker_overlay_handle
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Object.alonore_lock_position = bpy.props.BoolProperty(name='Lock Position', description='Exclude from position randomizer', default=False)
    bpy.types.Object.alonore_lock_power = bpy.props.BoolProperty(name='Lock Power', description='Exclude from power randomizer', default=False)
    bpy.types.Object.alonore_lock_color = bpy.props.BoolProperty(name='Lock Color', description='Exclude from color randomizers', default=False)
    for key in LIGHTS:
        setattr(bpy.types.Scene, 'alonore_' + key, bpy.props.PointerProperty(type=ALONORE_PG_light))
    bpy.types.Scene.alonore_show_legacy_details = bpy.props.BoolProperty(name='Show Legacy Light Panels', default=False)
    bpy.types.Scene.alonore_unified_selected = bpy.props.StringProperty(name='Selected Studio Light', default='')
    bpy.types.Scene.alonore_dynamic_entries = bpy.props.CollectionProperty(type=ALONORE_PG_dynamic_entry)
    bpy.types.Scene.alonore_power_scope = bpy.props.EnumProperty(name='Power Scope', items=[('DYNAMIC','Additional','Only added lights'),('LEGACY','Original','Original studio lights'),('ALL','All','Original and added lights')], default='ALL')
    bpy.types.Scene.alonore_power_seed = bpy.props.IntProperty(name='Power Seed', default=1, min=0)
    bpy.types.Scene.alonore_power_min = bpy.props.FloatProperty(name='Minimum Power %', default=70, min=0, max=500)
    bpy.types.Scene.alonore_power_max = bpy.props.FloatProperty(name='Maximum Power %', default=130, min=0, max=500)
    bpy.types.Scene.alonore_new_light_type = bpy.props.EnumProperty(name='New Light Type', items=DYNAMIC_TYPES, default='AREA')
    bpy.types.Scene.alonore_dynamic_index = bpy.props.IntProperty(name='Light Index', default=0, min=0)
    bpy.types.Scene.alonore_light_count = bpy.props.EnumProperty(name='Light Count', items=[('3','3',''),('4','4',''),('5','5','')], default='3', update=update_light_count)
    bpy.types.Scene.alonore_camera_padding = bpy.props.FloatProperty(name='Camera Padding', default=1.3, min=1.0, max=3.0)
    bpy.types.Scene.alonore_custom_name = bpy.props.StringProperty(name='Preset Name', default='My Lighting')
    bpy.types.Scene.alonore_active_preset = bpy.props.StringProperty(name='Active Preset', default='MODIFIED')
    bpy.types.Scene.alonore_preset_slot = bpy.props.EnumProperty(name='Preset Slot', items=[(str(i), 'Slot ' + str(i), '') for i in range(1, 7)], default='1')
    bpy.types.Scene.alonore_temperature_mode = bpy.props.EnumProperty(name='Temperature Scheme', items=[('RANDOM','Random Palette','Choose a warm/cool palette randomly'),('WARM_COOL','Warm Key / Cool Rim','Warm key and cold rim'),('COOL_WARM','Cool Key / Warm Rim','Cold key and warm rim'),('ICE_BLUE','Ice Blue','Predominantly cold lights'),('GOLDEN','Golden Hour','Warm golden lighting'),('NEUTRAL','Neutral Studio','Neutral light')], default='RANDOM')
    bpy.types.Scene.alonore_creative_palette = bpy.props.EnumProperty(name='Creative Palette', items=[('RANDOM','Random Creative','Random artistic palette'),('TEAL_ORANGE','Teal / Orange','Teal and warm orange'),('BLUE_AMBER','Blue / Amber','Blue and amber'),('MAGENTA_CYAN','Magenta / Cyan','Magenta and cyan'),('ICE_NEON','Ice Neon','Strong blue and cyan')], default='RANDOM')
    bpy.types.Scene.alonore_paint_light = bpy.props.EnumProperty(name='Active Softbox', items=[(k, k.title() + ' Light', '') for k in LIGHTS], default='key')
    bpy.types.Scene.alonore_paint_live = bpy.props.BoolProperty(name='Live Preview', default=False, description='Preview softbox placement while moving the mouse')
    bpy.types.Scene.alonore_paint_marker = bpy.props.BoolProperty(name='Target Marker', default=True, description='Show persistent saved target markers and preview crosshair', update=update_target_visibility)
    bpy.types.Scene.alonore_marker_size = bpy.props.FloatProperty(name='Marker Size', default=0.16, min=0.02, max=0.6, description='Marker size relative to studio object scale', update=update_target_visibility)
    bpy.types.Scene.alonore_marker_in_front = bpy.props.BoolProperty(name='Always On Top', default=True, description='Draw screen-space markers through geometry; off hides occluded markers', update=update_target_visibility)
    bpy.types.Scene.alonore_marker_labels = bpy.props.BoolProperty(name='Show Marker Names', default=True, description='Show the light name next to each marker', update=update_target_visibility)
    bpy.types.Scene.alonore_paint_distance = bpy.props.FloatProperty(name='Paint Distance', default=2.5, min=0.2, max=20.0, description='Distance from clicked surface in model-size units')
    bpy.types.Scene.alonore_softbox_seed = bpy.props.IntProperty(name='Softbox Seed', default=1, min=0, max=99999999)
    bpy.types.Scene.alonore_softbox_random_mode = bpy.props.EnumProperty(name='Softbox Random Mode', items=[('ALL','All Shapes','Randomize shapes and dimensions'),('ROUND','Round Only','Use round disk softboxes'),('STRIPS','Rectangles / Strips','Use rectangular softboxes'),('SIZES','Sizes Only','Preserve shapes and randomize dimensions')], default='ALL')
    bpy.types.Scene.alonore_random_seed = bpy.props.IntProperty(name='Random Seed', default=1, min=0, max=99999999)
    bpy.types.Scene.alonore_light_multiplier = bpy.props.FloatProperty(
        name='Master Energy', default=1.0, min=0, max=5, update=set_active_modified)
    if _marker_overlay_handle is None:
        _marker_overlay_handle = bpy.types.SpaceView3D.draw_handler_add(draw_saved_markers, (), 'WINDOW', 'POST_PIXEL')
    # Blender restricts bpy.data access during add-on registration.
    # Existing marker data is read lazily by the viewport draw callback.


def unregister():
    global _marker_overlay_handle
    if _marker_overlay_handle is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_marker_overlay_handle, 'WINDOW')
        _marker_overlay_handle = None
    del bpy.types.Object.alonore_lock_position
    del bpy.types.Object.alonore_lock_power
    del bpy.types.Object.alonore_lock_color
    del bpy.types.Scene.alonore_show_legacy_details
    del bpy.types.Scene.alonore_unified_selected
    del bpy.types.Scene.alonore_dynamic_entries
    del bpy.types.Scene.alonore_power_scope
    del bpy.types.Scene.alonore_power_seed
    del bpy.types.Scene.alonore_power_min
    del bpy.types.Scene.alonore_power_max
    del bpy.types.Scene.alonore_new_light_type
    del bpy.types.Scene.alonore_dynamic_index
    del bpy.types.Scene.alonore_light_count
    del bpy.types.Scene.alonore_camera_padding
    del bpy.types.Scene.alonore_custom_name
    del bpy.types.Scene.alonore_active_preset
    del bpy.types.Scene.alonore_preset_slot
    del bpy.types.Scene.alonore_temperature_mode
    del bpy.types.Scene.alonore_creative_palette
    del bpy.types.Scene.alonore_paint_light
    del bpy.types.Scene.alonore_paint_live
    del bpy.types.Scene.alonore_paint_marker
    del bpy.types.Scene.alonore_marker_size
    del bpy.types.Scene.alonore_marker_in_front
    del bpy.types.Scene.alonore_marker_labels
    del bpy.types.Scene.alonore_paint_distance
    del bpy.types.Scene.alonore_softbox_seed
    del bpy.types.Scene.alonore_softbox_random_mode
    del bpy.types.Scene.alonore_random_seed
    del bpy.types.Scene.alonore_light_multiplier
    for key in LIGHTS:
        delattr(bpy.types.Scene, 'alonore_' + key)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == '__main__':
    register()
