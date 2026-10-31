"""Phase 0 Blender bench (31 Oct 2026). Synthetic content only, nothing from IA.

Builds a 3 s, 1920x1080, 30 fps test: 6 flat unlit cards at different depths, an eased camera
dolly-in (push in, easeAE(51, 100) from research/ia-storage-wars/skills/motion/RULES.md), a rack
focus (real DOF), motion blur, compositor glow + film grain, Standard view transform, and one
Grease Pencil marker scribble that draws on over 0.6 s. Renders frame by frame and writes timing.

Run headless:  blender -b -P bench_phase0.py -- --engine cycles --samples 16 --out /tmp/frames
"""
import bpy, sys, os, json, math, time, argparse, platform
import numpy as np

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--engine", default="cycles", choices=["cycles", "eevee"])
ap.add_argument("--samples", type=int, default=16)
ap.add_argument("--out", default="/tmp/frames")
ap.add_argument("--frames", default="1-90")
a = ap.parse_args(argv)
os.makedirs(a.out, exist_ok=True)
notes = []

FPS, W, H = 30, 1920, 1080


def ease_ae(o, i):
    """AE influence ease -> cubic bezier (o, 0, 1 - i, 1); returns f(t) for t in 0..1."""
    x1, x2 = o / 100.0, 1 - i / 100.0
    def bez(p1, p2, s): return 3 * (1 - s) ** 2 * s * p1 + 3 * (1 - s) * s * s * p2 + s ** 3
    def f(t):
        lo, hi = 0.0, 1.0
        for _ in range(40):
            m = (lo + hi) / 2
            if bez(x1, x2, m) < t: lo = m
            else: hi = m
        return bez(0.0, 1.0, (lo + hi) / 2)
    return f


PUSH = ease_ae(51, 100)  # measured push-in default, dur 2.0 s

# ---------- clean scene ----------
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = W, H, 100
sc.render.fps = FPS
f0, f1 = (int(x) for x in a.frames.split("-"))
sc.frame_start, sc.frame_end = 1, 90
sc.view_settings.view_transform = "Standard"
sc.view_settings.look = "None"
sc.render.image_settings.file_format = "PNG"
sc.render.image_settings.color_mode = "RGB"
sc.render.use_motion_blur = True
sc.render.motion_blur_shutter = 0.5

world = bpy.data.worlds.new("w"); sc.world = world
world.use_nodes = True
world.node_tree.nodes["Background"].inputs[0].default_value = (0.012, 0.011, 0.010, 1)

# ---------- textures (generated, synthetic) ----------
rng = np.random.default_rng(7)


def paper_tex(name, base, ink, accent, seed):
    r = np.random.default_rng(seed)
    w, h = 1200, 900
    img = np.ones((h, w, 4), np.float32); img[..., :3] = base
    img[..., :3] *= (1 - 0.06 * r.random((h, w, 1))).astype(np.float32)  # paper tooth
    y = h - 90  # numpy rows go bottom-up in Blender, so draw from the top down
    img[y - 70:y, 70:w - 260, :3] = ink  # headline bar
    y -= 120
    for k in range(9):  # text lines
        ln = int(w * (0.55 + 0.3 * r.random()))
        img[y - 14:y, 70:ln, :3] = ink * 0.9 + base * 0.1
        y -= 38
    img[90:90 + 260, w - 520:w - 70, :3] = accent  # photo block
    g = np.linspace(0.6, 1.0, 260, dtype=np.float32)[:, None, None]
    img[90:350, w - 520:w - 70, :3] *= g
    im = bpy.data.images.new(name, w, h, alpha=False)
    im.pixels.foreach_set(img.ravel()); im.pack()
    return im


PAL = [((0.93, 0.90, 0.83), (0.10, 0.09, 0.08), (0.75, 0.32, 0.12)),
       ((0.80, 0.70, 0.52), (0.15, 0.10, 0.06), (0.30, 0.35, 0.40)),
       ((0.86, 0.86, 0.84), (0.05, 0.05, 0.06), (0.62, 0.15, 0.12)),
       ((0.70, 0.78, 0.82), (0.06, 0.08, 0.12), (0.92, 0.85, 0.70)),
       ((0.95, 0.93, 0.88), (0.30, 0.08, 0.06), (0.20, 0.22, 0.20)),
       ((0.20, 0.20, 0.22), (0.92, 0.90, 0.85), (0.85, 0.55, 0.15))]


def unlit_mat(name, image=None, color=None, strength=1.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    nt = m.node_tree; nt.nodes.clear()
    out = nt.nodes.new("ShaderNodeOutputMaterial"); em = nt.nodes.new("ShaderNodeEmission")
    em.inputs["Strength"].default_value = strength
    if image is not None:
        tx = nt.nodes.new("ShaderNodeTexImage"); tx.image = image; tx.interpolation = "Cubic"
        nt.links.new(tx.outputs["Color"], em.inputs["Color"])
    else:
        em.inputs["Color"].default_value = (*color, 1)
    nt.links.new(em.outputs[0], out.inputs["Surface"])
    return m


def card(name, mat, loc, size, rot_y=0.0):
    bpy.ops.mesh.primitive_plane_add(size=1, location=loc, rotation=(math.radians(90), rot_y, 0))
    ob = bpy.context.active_object; ob.name = name
    ob.scale = (size[0], size[1], 1); ob.data.materials.append(mat)
    return ob


# depth runs along +Y away from the camera
LAYOUT = [((-1.6, 0.0, 0.35), 1.0), ((1.9, 1.6, -0.5), 1.25), ((-0.2, 3.2, 0.9), 1.4),
          ((-3.0, 5.0, -0.9), 1.6), ((2.6, 7.0, 1.4), 1.8), ((0.0, 9.5, -0.2), 2.4)]
cards = []
for k, ((x, y, z), s) in enumerate(LAYOUT):
    im = paper_tex(f"tex{k}", *[np.array(c, np.float32) for c in PAL[k]], seed=k)
    cards.append(card(f"card{k}", unlit_mat(f"m{k}", im), (x, y, z), (1.333 * s, s), rot_y=math.radians([-3, 2, -1.5, 4, -2, 1][k])))
# one hot accent bar so the glow has something to bloom
card("hot", unlit_mat("hot", color=(1.0, 0.55, 0.18), strength=6.0), (2.6, 6.95, 0.55), (1.6, 0.08))
card("backdrop", unlit_mat("bd", color=(0.035, 0.032, 0.03)), (0, 14, 0), (60, 34))

# ---------- camera: dolly in + rack focus ----------
cam_d = bpy.data.cameras.new("cam"); cam = bpy.data.objects.new("cam", cam_d)
sc.collection.objects.link(cam); sc.camera = cam
cam.rotation_euler = (math.radians(90), 0, 0)
cam_d.lens = 50
cam_d.dof.use_dof = True
cam_d.dof.aperture_fstop = 1.0
foc = bpy.data.objects.new("focus", None); sc.collection.objects.link(foc)
cam_d.dof.focus_object = foc

Y0, Y1 = -11.0, -6.2           # zoom ~1.5x on the near cards (RULES push in ~1.57)
D0, D1 = 1, 1 + int(2.0 * FPS)   # dolly 2.0 s from the first frame, then hold
R0, R1 = 34, 34 + int(1.4 * FPS)  # rack focus far card -> near card
far, near = cards[4].location.copy(), cards[0].location.copy()
for f in range(1, 91):
    t = min(max((f - D0) / (D1 - D0), 0), 1)
    cam.location = (0.25 * PUSH(t), Y0 + (Y1 - Y0) * PUSH(t), 0.15 - 0.1 * PUSH(t))
    cam.keyframe_insert("location", frame=f)
    u = PUSH(min(max((f - R0) / (R1 - R0), 0), 1))
    foc.location = far.lerp(near, u)
    foc.keyframe_insert("location", frame=f)

# ---------- grease pencil marker scribble (draws on over 0.6 s) ----------
try:
    gpd_coll = getattr(bpy.data, "grease_pencils_v3", None) or bpy.data.grease_pencils
    gpd = gpd_coll.new("scribble")
    gob = bpy.data.objects.new("scribble", gpd); sc.collection.objects.link(gob)
    gm = bpy.data.materials.new("marker"); bpy.data.materials.create_gpencil_data(gm)
    gm.grease_pencil.color = (0.86, 0.12, 0.08, 1.0)
    gpd.materials.append(gm)
    # a loose loop circling card0's headline, slightly in front of it
    cx, cy, cz = near.x, near.y - 0.05, near.z + 0.32
    N = 70
    pts = []
    for i in range(N):
        s = i / (N - 1); ang = math.radians(200) + s * math.radians(400)
        wob = 1 + 0.06 * math.sin(s * 23)
        pts.append((cx + 0.62 * wob * math.cos(ang), cy, cz + 0.17 * wob * math.sin(ang) + 0.05 * s))
    layer = gpd.layers.new("ink")
    layer.frames.new(1)  # empty until the draw-on starts
    S0, SN = 50, int(0.6 * FPS)
    for k in range(1, SN + 1):
        n = max(2, round(N * PUSH(k / SN)))
        fr = layer.frames.new(S0 + k - 1)
        fr.drawing.add_strokes([n])
        st = fr.drawing.strokes[0]
        for j, p in enumerate(st.points):
            p.position = pts[j]; p.radius = 0.012
    notes.append("grease pencil: ok")
except Exception as e:  # keep the bench going, report it
    notes.append(f"grease pencil failed: {type(e).__name__}: {e}")

# ---------- engine ----------
if a.engine == "cycles":
    sc.render.engine = "CYCLES"
    cy = sc.cycles
    cy.device = "CPU"; cy.samples = a.samples
    cy.use_adaptive_sampling = True; cy.adaptive_threshold = 0.05
    cy.use_denoising = True; cy.denoiser = "OPENIMAGEDENOISE"
    try: cy.denoising_prefilter = "FAST"
    except Exception: pass
    cy.max_bounces = 1; cy.diffuse_bounces = cy.glossy_bounces = cy.transmission_bounces = 0
    cy.volume_bounces = cy.transparent_max_bounces = 0
    cy.caustics_reflective = cy.caustics_refractive = False
    cy.use_light_tree = False
    sc.render.use_persistent_data = True
else:
    sc.render.engine = "BLENDER_EEVEE_NEXT" if "BLENDER_EEVEE_NEXT" in [e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items] else "BLENDER_EEVEE"
    sc.eevee.taa_render_samples = a.samples
    try: sc.eevee.motion_blur_steps = 1
    except Exception: pass

# ---------- compositor: glow + film grain ----------
sc.use_nodes = True
nt = sc.node_tree; nt.nodes.clear()
rl = nt.nodes.new("CompositorNodeRLayers"); comp = nt.nodes.new("CompositorNodeComposite")


def setp(node, name, val):
    """4.5 moved some node properties to input sockets; set whichever exists."""
    if hasattr(node, name.lower().replace(" ", "_")):
        try: setattr(node, name.lower().replace(" ", "_"), val); return
        except Exception: pass
    if name in node.inputs: node.inputs[name].default_value = val


gl = nt.nodes.new("CompositorNodeGlare")
setp(gl, "glare_type", "FOG_GLOW")
for k, v in (("Threshold", 1.0), ("Size", 0.5)):
    try: setp(gl, k, v)
    except Exception: pass
try: setp(gl, "quality", "MEDIUM")
except Exception: pass
nt.links.new(rl.outputs["Image"], gl.inputs["Image"])
last = gl.outputs["Image"]
try:
    tex = bpy.data.textures.new("grain", "NOISE")
    tn = nt.nodes.new("CompositorNodeTexture"); tn.texture = tex
    mix = nt.nodes.new("CompositorNodeMixRGB"); mix.blend_type = "OVERLAY"
    mix.inputs[0].default_value = 0.07
    nt.links.new(last, mix.inputs[1]); nt.links.new(tn.outputs["Color"], mix.inputs[2])
    last = mix.outputs["Image"]
    notes.append("grain: compositor noise texture")
except Exception as e:
    notes.append(f"grain node failed ({type(e).__name__}); ffmpeg grain used instead")
nt.links.new(last, comp.inputs["Image"])

# ---------- render frame by frame with timing ----------
times = []
t_all = time.time()
for f in range(f0, f1 + 1):
    sc.frame_set(f)
    sc.render.filepath = os.path.join(a.out, f"f_{f:04d}.png")
    t = time.time()
    bpy.ops.render.render(write_still=True)
    times.append(round(time.time() - t, 3))
    print(f"frame {f} {times[-1]} s", flush=True)
wall = time.time() - t_all
rep = {"engine": sc.render.engine, "samples": a.samples, "frames": len(times),
       "sec_per_frame_mean": round(sum(times) / len(times), 3),
       "sec_per_frame_median": sorted(times)[len(times) // 2],
       "first_frame_s": times[0], "render_wall_s": round(wall, 1),
       "cpus": os.cpu_count(), "blender": bpy.app.version_string, "machine": platform.platform(),
       "notes": notes, "per_frame_s": times}
with open(os.path.join(a.out, "timing.json"), "w") as fh:
    json.dump(rep, fh, indent=1)
print(json.dumps({k: v for k, v in rep.items() if k != "per_frame_s"}), flush=True)
