# The text-to-image graph in ComfyUI's API format: the same as the txt2img and txt2img-lora templates in the UI,
# with a chain of LoRA between the checkpoint and everything that uses it. No LoRA gives plain txt2img.


def txt2img(params, seed, prefix):
    nodes = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": params["checkpoint"]}}}
    model, clip = ["1", 0], ["1", 1]
    for i, lora in enumerate(params["loras"]):
        node = str(10 + i)
        nodes[node] = {"class_type": "LoraLoader", "inputs": {
            "model": model, "clip": clip, "lora_name": lora["file"],
            "strength_model": lora["strength"], "strength_clip": lora["strength"]}}
        model, clip = [node, 0], [node, 1]
    return nodes | {
        "2": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": params["prompt"]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"clip": clip, "text": params["negative"]}},
        "4": {"class_type": "EmptyLatentImage",
              "inputs": {"width": params["width"], "height": params["height"], "batch_size": 1}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": model, "positive": ["2", 0], "negative": ["3", 0], "latent_image": ["4", 0],
            "seed": seed, "steps": params["steps"], "cfg": params["cfg"],
            "sampler_name": params["sampler"], "scheduler": params["scheduler"], "denoise": 1}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": prefix}},
    }
