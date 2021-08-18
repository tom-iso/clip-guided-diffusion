import argparse
import sys
from functools import lru_cache
from pathlib import Path

import clip
import torch as th
from PIL import Image
from torchvision import transforms as tvt
from torchvision.transforms import functional as tf

from data.imagenet1000_clsidx_to_labels import IMAGENET_CLASSES
from cgd_util import MakeCutouts, download_guided_diffusion, fetch, load_guided_diffusion, log_image, spherical_dist_loss, tv_loss, txt_to_dir

sys.path.append("./guided-diffusion")

TIMESTEP_RESPACINGS = ("25", "50", "100", "250", "500", "1000",
                       "ddim25", "ddim50", "ddim100", "ddim250", "ddim500", "ddim1000")
DIFFUSION_SCHEDULES = (25, 50, 100, 250, 500, 1000)
IMAGE_SIZES = (64, 128, 256, 512)
CLIP_MODEL_NAMES = ("ViT-B/16", "ViT-B/32", "RN50",
                    "RN101", "RN50x4", "RN50x16")

CLIP_NORMALIZE = tvt.Normalize(mean=[0.48145466, 0.4578275, 0.40821073], std=[
                               0.26862954, 0.26130258, 0.27577711])


@lru_cache(maxsize=None)
def imagenet_top_n(prompt, prompt_min='', min_weight=0.1, clip_model=None, device=None, n: int = len(IMAGENET_CLASSES)):
    with th.no_grad():
        imagenet_lbl_tokens = clip.tokenize(IMAGENET_CLASSES).to(device)
        prompt_tokens = clip.tokenize(prompt).to(device)
        imagenet_features = clip_model.encode_text(imagenet_lbl_tokens).float()
        prompt_features = clip_model.encode_text(prompt_tokens).float()
        imagenet_features /= imagenet_features.norm(dim=-1, keepdim=True)
        prompt_features /= prompt_features.norm(dim=-1, keepdim=True)
        if len(prompt_min) > 0:
            prompt_min_tokens = clip.tokenize(prompt_min).to(device)
            prompt_min_features = clip_model.encode_text(
                prompt_min_tokens).float()
            prompt_min_features /= prompt_min_features.norm(
                dim=-1, keepdim=True)
            prompt_features = prompt_features - \
                (min_weight * prompt_min_features)
        text_probs = (100.0 * prompt_features @
                      imagenet_features.T).softmax(dim=-1)
        sorted_probs, sorted_classes = text_probs.cpu().topk(n, dim=-1, sorted=True)
        categorical_clip_scores = th.distributions.Categorical(sorted_probs)
        return (sorted_classes[0], categorical_clip_scores)


def clip_guided_diffusion(
    prompt: str,
    prompt_min: str = None,
    min_weight: float = 0.1,
    batch_size: int = 1,
    tv_scale: float = 100,
    top_n: int = len(IMAGENET_CLASSES),
    image_size: int = 128,
    class_cond: bool = False,
    clip_guidance_scale: float = 1000,
    cutout_power: float = 1.0,
    num_cutouts: int = 16,
    timestep_respacing: str = "1000",
    custom_device: str = None,
    seed: int = None,
    diffusion_steps: int = 1000,
    skip_timesteps: int = 0,
    init_image: str = None,
    checkpoints_dir: Path = Path("./checkpoints"),
    clip_model_name: str = "ViT-B/32",
    class_score: bool = False,
    augs: list = [],
):
    assert timestep_respacing in TIMESTEP_RESPACINGS, f"timestep_respacing should be one of {TIMESTEP_RESPACINGS}"
    assert diffusion_steps in DIFFUSION_SCHEDULES, f"Diffusion steps should be one of: {DIFFUSION_SCHEDULES}"
    assert clip_model_name in CLIP_MODEL_NAMES, f"clip model name should be one of: {CLIP_MODEL_NAMES}"
    assert image_size in IMAGE_SIZES, f"image size should be one of {IMAGE_SIZES}"
    assert num_cutouts > 0, "--num_cutouts/-cutn must greater than zero."
    device = th.device("cuda:0") if th.cuda.is_available() else "cpu"
    if custom_device:
        device = th.device(custom_device)

    # Assertions
    assert len(prompt) > 0, "--prompt/-txt cant be empty"
    assert 0 < top_n <= len(
        IMAGENET_CLASSES), f"top_n must be less than or equal to the number of classes: {top_n} > {len(IMAGENET_CLASSES)}"
    assert 0.0 <= min_weight <= 1.0, f"min_weight must be between 0 and 1: {min_weight} not in [0, 1]"
    assert (not class_cond and image_size ==
            256) or class_cond, f"Image size must be 256 when --class_cond/-cond is False."
    if init_image:
        # Check skip timesteps logic
        assert skip_timesteps > 0 and skip_timesteps < int(timestep_respacing.replace("ddim", "")), \
            f"--skip_timesteps/-skip (currently {skip_timesteps}) must be greater than 0 and less than --timestep_respacing/-respace (currently {timestep_respacing}) when --init_image/-init is not None."
        assert Path(init_image).exists(
        ), f"{init_image} does not exist. Check spelling or provide another path."
    else:
        assert skip_timesteps == 0, f"--skip_timesteps/-skip must be 0 when --init_image/-init is None."

    assert Path(checkpoints_dir).is_dir(
    ), f"--checkpoints_dir/-ckpts {checkpoints_dir} is a file, not a directory. Please provide a directory."
    assert Path(checkpoints_dir).exists(
    ), f"--checkpoints_dir/-ckpts {checkpoints_dir} does not exist. Create it or provide another directory."
    # Setup
    if seed:
        th.manual_seed(seed)

    # Download pretrained Guided Diffusion model
    checkpoints_dir = Path(checkpoints_dir)
    diffusion_path = download_guided_diffusion(
        image_size=image_size, checkpoints_dir=checkpoints_dir, class_cond=class_cond)

    # Load CLIP model
    clip_model = clip.load(clip_model_name, jit=False)[
        0].eval().requires_grad_(False).to(device)
    clip_size = clip_model.visual.input_resolution

    # Use CLIP scores as weights for random class selection.
    model_kwargs = {}
    model_kwargs["y"] = th.zeros([batch_size], device=device, dtype=th.long)
    # Rank the classes by their CLIP score
    clip_scores = imagenet_top_n(
        prompt, prompt_min, min_weight, clip_model, device, top_n) if class_score else None
    if clip_scores is not None:
        print(f"Ranking top {top_n} ImageNet classes by their CLIP score.")
    else:
        print("Ranking all ImageNet classes uniformly. Use --class_score/-score to enable CLIP guided class selection instead.")

    # Setup CLIP cutouts/embeds
    make_cutouts = MakeCutouts(
        clip_size, num_cutouts, cutout_size_power=cutout_power, augment_list=augs)
    text_embed = clip_model.encode_text(
        clip.tokenize(prompt).to(device)).float()
    text_min_embed = clip_model.encode_text(clip.tokenize(
        prompt_min).to(device)).float() if prompt_min else None

    # Load initial image (if provided)
    init_tensor = None
    if init_image:
        pil_image = Image.open(fetch(init_image)).convert(
            "RGB").resize((image_size, image_size), Image.LANCZOS)
        init_tensor = tf.to_tensor(pil_image).to(
            device).unsqueeze(0).mul(2).sub(1)

    # Load guided diffusion
    gd_model, diffusion = load_guided_diffusion(
        checkpoint_path=diffusion_path,
        image_size=image_size,
        diffusion_steps=diffusion_steps,
        timestep_respacing=timestep_respacing,
        device=device,
        class_cond=class_cond,
    )

    # Customize guided-diffusion model with function that uses CLIP guidance.
    current_timestep = diffusion.num_timesteps - 1

    def cond_fn(x, t, y=None):
        with th.enable_grad():
            x = x.detach().requires_grad_()
            n = x.shape[0]
            my_t = th.ones([n], device=device, dtype=th.long) * \
                current_timestep
            out = diffusion.p_mean_variance(
                gd_model, x, my_t, clip_denoised=False, model_kwargs={"y": y})
            fac = diffusion.sqrt_one_minus_alphas_cumprod[current_timestep]
            x_in = out["pred_xstart"] * fac + x * (1 - fac)
            clip_in = CLIP_NORMALIZE(make_cutouts(x_in.add(1).div(2)))
            cutout_embeds = clip_model.encode_image(
                clip_in).float().view([num_cutouts, n, -1])
            max_dists = spherical_dist_loss(
                cutout_embeds, text_embed.unsqueeze(0))
            if text_min_embed is not None:  # Implicit comparison to None is not supported by pytorch tensors
                min_dists = spherical_dist_loss(
                    cutout_embeds, text_min_embed.unsqueeze(0))
                dists = max_dists - (min_weight * min_dists)
            else:
                dists = max_dists
            losses = dists.mean(0)
            tv_losses = tv_loss(x_in)
            loss = losses.sum() * clip_guidance_scale + tv_losses.sum() * tv_scale
            return -th.autograd.grad(loss, x)[0]

    if timestep_respacing.startswith("ddim"):
        diffusion_sample_loop = diffusion.ddim_sample_loop_progressive
    else:
        diffusion_sample_loop = diffusion.p_sample_loop_progressive

    samples = diffusion_sample_loop(
        gd_model, (batch_size, 3, image_size, image_size),
        clip_denoised=False, model_kwargs=model_kwargs, cond_fn=cond_fn,
        progress=True, skip_timesteps=skip_timesteps, init_image=init_tensor,
        randomize_class=class_cond, clip_scores=clip_scores,
    )
    return samples, gd_model, diffusion


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    p.add_argument("--prompt", "-txt", type=str,
                   default='', help="the prompt to reward")
    p.add_argument("--prompt_min", "-min", type=str,
                   default=None, help="the prompt to penalize")
    p.add_argument("--min_weight", "-min_wt", type=str,
                   default=0.1, help="the prompt to penalize")
    p.add_argument("--image_size", "-size", type=int, default=128,
                   help="Diffusion image size. Must be one of [64, 128, 256, 512].")
    p.add_argument("--init_image", "-init", type=str,
                   help="Blend an image with diffusion for n steps")
    p.add_argument("--skip_timesteps", "-skip", type=int, default=0,
                   help="Number of timesteps to blend image for. CLIP guidance occurs after this.")
    p.add_argument("--prefix", "-dir", default="outputs",
                   type=Path, help="output directory")
    p.add_argument("--checkpoints_dir", "-ckpts", default='checkpoints',
                   type=Path, help="Path subdirectory containing checkpoints.")
    p.add_argument("--batch_size", "-bs", type=int,
                   default=1, help="the batch size")
    p.add_argument("--clip_guidance_scale", "-cgs", type=float, default=1000,
                   help="Scale for CLIP spherical distance loss. Values will need tinkering for different settings.",)
    p.add_argument("--tv_scale", "-tvs", type=float,
                   default=100, help="Scale for denoising loss",)
    p.add_argument("--class_score", "-score", action="store_true",
                   help="Enables CLIP guided class randomization.",)
    p.add_argument("--top_n", "-top", type=int, default=len(IMAGENET_CLASSES),
                   help="Top n imagenet classes compared to phrase by CLIP",)
    p.add_argument("--seed", "-seed", type=int,
                   default=0, help="Random number seed")
    p.add_argument("--save_frequency", "-freq", type=int,
                   default=5, help="Save frequency")
    p.add_argument("--device", type=str,
                   help="device to run on .e.g. cuda:0 or cpu")
    p.add_argument("--diffusion_steps", "-steps", type=int,
                   default=1000, help="Diffusion steps")
    p.add_argument("--timestep_respacing", "-respace", type=str,
                   default="1000", help="Timestep respacing")
    p.add_argument("--num_cutouts", "-cutn", type=int, default=32,
                   help="Number of randomly cut patches to distort from diffusion.")
    p.add_argument("--cutout_power", "-cutpow", type=float,
                   default=0.5, help="Cutout size power")
    p.add_argument("--clip_model", "-clip", type=str, default="ViT-B/32",
                   help=f"clip model name. Should be one of: {CLIP_MODEL_NAMES}")
    p.add_argument("--class_cond", "-cond", type=bool, default=True,
                   help="Use class conditional. Required for image sizes other than 256")
    args = p.parse_args()

    assert 0 < args.save_frequency <= int(args.timestep_respacing.replace('ddim', '')), \
        "--save_frequency/--freq must be greater than 0and less than --timestep_respacing"

    # convert Path arg to Path object
    prefix_path = Path(args.prefix)
    prefix_path.mkdir(exist_ok=True)
    assert prefix_path.is_dir(
    ), f"--prefix,-dir {args.prefix} is a file, not a directory. Please provide a directory."

    # Initialize diffusion generator
    cgd_samples, _, diffusion = clip_guided_diffusion(
        prompt=args.prompt,
        prompt_min=args.prompt_min,
        min_weight=args.min_weight,
        batch_size=args.batch_size,
        tv_scale=args.tv_scale,
        top_n=args.top_n,
        image_size=args.image_size,
        class_cond=args.class_cond,
        clip_guidance_scale=args.clip_guidance_scale,
        cutout_power=args.cutout_power,
        num_cutouts=args.num_cutouts,
        timestep_respacing=args.timestep_respacing,
        seed=args.seed,
        custom_device=args.device,
        diffusion_steps=args.diffusion_steps,
        skip_timesteps=args.skip_timesteps,
        init_image=args.init_image,
        checkpoints_dir=args.checkpoints_dir,
        clip_model_name=args.clip_model,
        class_score=args.class_score,
    )

    # Remove non-alphanumeric and white space characters from prompt and prompt_min for directory name
    outputs_path = txt_to_dir(base_path=prefix_path,
                              txt=args.prompt, txt_min=args.prompt_min)
    outputs_path.mkdir(exist_ok=True)

    try:
        current_timestep = diffusion.num_timesteps - 1
        for step, sample in enumerate(cgd_samples):
            current_timestep -= 1
            if step % args.save_frequency == 0 or current_timestep == -1:
                for j, image in enumerate(sample["pred_xstart"]):
                    log_image(image, prefix_path, step, j)
    except RuntimeError as runtime_ex:
        if "CUDA out of memory" in str(runtime_ex):
            print(f"CUDA OOM error occurred.")
            print(
                f"Try lowering --image_size/-size, --batch_size/-bs, --num_cutouts/-cutn")
            print(
                f"--clip_model/-clip (currently {args.clip_model}) can have a large impact on VRAM usage.")
            print(
                f"RN50 will use the least VRAM. ViT-B/32 is the best bang for your buck.")
        else:
            raise runtime_ex


if __name__ == "__main__":
    main()
