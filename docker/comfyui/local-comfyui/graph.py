# Graphs in ComfyUI's API format. txt2img is the same as the txt2img, txt2img-lora, txt2img-pose and sprite templates
# in the UI: a chain of LoRA between the checkpoint and everything that uses it, a pose ControlNet between the prompts
# and the sampler when a pose is given, a starting picture instead of noise when one is given (img2img), only part of
# it redrawn when it comes with a mask (inpainting), and background removal after decoding when post-processing is
# asked for. Without them it is plain txt2img.

# Outputs the API reads back: the picture and, for a sprite, the foreground mask
IMAGE, MASK = "7", "33"


def txt2img(params, seed, prefix):
    nodes = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": params["checkpoint"]}}}
    model, clip = ["1", 0], ["1", 1]
    for i, lora in enumerate(params["loras"]):
        node = str(10 + i)
        nodes[node] = {"class_type": "LoraLoader", "inputs": {
            "model": model, "clip": clip, "lora_name": lora["file"],
            "strength_model": lora["strength"], "strength_clip": lora["strength"]}}
        model, clip = [node, 0], [node, 1]
    positive, negative = ["2", 0], ["3", 0]
    if pose := params.get("pose"):
        nodes |= {
            "20": {"class_type": "ControlNetLoader", "inputs": {"control_net_name": pose["model"]}},
            "21": {"class_type": "LoadImage", "inputs": {"image": pose["image"]}},
            "22": {"class_type": "ControlNetApplyAdvanced", "inputs": {
                "positive": positive, "negative": negative, "control_net": ["20", 0], "image": ["21", 0],
                "strength": pose["strength"], "start_percent": pose["start"], "end_percent": pose["end"],
                "vae": ["1", 2]}},
        }
        positive, negative = ["22", 0], ["22", 1]
    if post := params.get("post"):
        nodes |= foreground_mask(["6", 0], post["model"], prefix + "_mask")
    if init := params.get("init"):
        # img2img: sampling starts from the encoded picture instead of noise, and goes only part of the way
        nodes |= {
            "40": {"class_type": "LoadImage", "inputs": {"image": init["image"]}},
            "41": {"class_type": "VAEEncode", "inputs": {"pixels": ["40", 0], "vae": ["1", 2]}},
        }
        latent, denoise = ["41", 0], init["denoise"]
        if init.get("mask"):
            # Only the white of the mask is sampled; the rest of the picture is kept at every step
            nodes |= {
                "42": {"class_type": "LoadImageMask", "inputs": {"image": init["mask"], "channel": "red"}},
                "43": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": latent, "mask": ["42", 0]}},
            }
            latent = ["43", 0]
    else:
        nodes["4"] = {"class_type": "EmptyLatentImage",
                      "inputs": {"width": params["width"], "height": params["height"], "batch_size": 1}}
        latent, denoise = ["4", 0], 1
    return nodes | {
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": params["prompt"]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": params["negative"]}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": model, "positive": positive, "negative": negative, "latent_image": latent,
            "seed": seed, "steps": params["steps"], "cfg": params["cfg"],
            "sampler_name": params["sampler"], "scheduler": params["scheduler"], "denoise": denoise}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        IMAGE: {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": prefix}},
    }


def remove_background(image, model, prefix):
    # For a picture sent to /tool/post: only its foreground mask
    return {"1": {"class_type": "LoadImage", "inputs": {"image": image}}} | foreground_mask(["1", 0], model, prefix)


def foreground_mask(image, model, prefix):
    # The mask is saved as a picture of its own (white is the figure); the sprite is put together from it in post.py
    return {
        "30": {"class_type": "LoadBackgroundRemovalModel", "inputs": {"bg_removal_name": model}},
        "31": {"class_type": "RemoveBackground", "inputs": {"bg_removal_model": ["30", 0], "image": image}},
        "32": {"class_type": "MaskToImage", "inputs": {"mask": ["31", 0]}},
        MASK: {"class_type": "SaveImage", "inputs": {"images": ["32", 0], "filename_prefix": prefix}},
    }
