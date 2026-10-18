# Generate Image

Create new images from text prompts, or edit photos the user has sent.

## When to Use

**USE this skill when:**
- User asks you to draw, create, or generate a picture/illustration
- User asks you to edit a photo they sent (change colors, remove/add objects, restyle)
- User asks for an image for a specific purpose (avatar, poster, icon, wallpaper)

**DON'T use this skill when:**
- User asks what's IN an image → use analyze_image
- User asks to print an image → use the print skill, if one is available
- Image generation is disabled (the tool returns "Image generation is disabled")

## How to Use

1. Compose a rich, detailed prompt YOURSELF: subject, style, lighting, composition, colors. Do not write vague one-liners.
2. Call generate_image with:
   - `prompt`: your detailed prompt
   - `input_images` (optional): a list of filenames from the attachments/ directory to use as reference images for an edit. Send multiple files to combine/merge images (up to 8 by default, configurable via `image_gen.max_input_images`). A single filename string is also accepted.
   - `size` (optional): "WxH". Omit it for the default 1024x1024 (~12 s), which is fine for most images. Edits: omit it, so the output keeps the input image's aspect ratio. See Performance workflow for other sizes.
   - `steps`: leave it out. The current backend runs a fixed number of passes and ignores it.
3. The tool returns a filename. Send it to the user with send_signal_attachment using that exact filename (add a short caption if you like).

## Performance workflow

**New image generation:**
- Generate at ~1 MP first, matching the user's requested aspect ratio: omit size (square 1024x1024), or 1184x896 (4:3 landscape), 896x1184 (3:4 portrait), 1344x768 (16:9), 768x1344 (9:16). Takes ~12 s.
- If the user asks for a specific aspect ratio at full detail, or an explicit size, use it directly. Native 2K presets (~1 min each): 2048x2048 (1:1), 2400x1792 (4:3), 1792x2400 (3:4), 2528x1696 (3:2), 1696x2528 (2:3), 2752x1536 (16:9), 1536x2752 (9:16). Max edge 2752.
- On user-requested modifications: edit the previous output - pass it as `input_images` and omit size.
- Never mention the preview size to the user; never offer a larger version. Act only on explicit "bigger / higher resolution" requests.
- On explicit "bigger" request: image-edit the last output instead of regenerating - put that file in `input_images` and prompt: "Upscale this image so its longest edge is 2048 px, keeping the aspect ratio. Keep all content, composition, and style exactly the same." Much faster than a fresh large generation.

**Image editing:**
- The baseline image must be the FIRST entry in `input_images`.
- If it's not clear which image is the reference (e.g. user sent two photos and said "edit the picture") -> stop and ask first. If the user names the image ("combine A and B"), proceed.
- Describe WHAT to change, not WHERE: no coordinates or object locations. The backend model locates objects itself.

**Quality vs speed**
- Do not pass `steps` - the current backend ignores it.
- For more detail, use a larger size (a 2K preset); it costs roughly linear time in pixels (~12 s at 1 MP, ~1 min at 4 MP).

**When it fails**
- "insufficient GPU memory": retry once right away with the size the error suggests (or without size), and tell the user the image is smaller than asked. Schedule a later retry only if that fails too.
- Timeout: the service is busy, not down. Do not repeat the same size; omit size or use a smaller one.

## Delivery

- Send the generated image to the user directly with send_signal_attachment. Do NOT run it through analyze_image or any other inspection first.
- Analyze only when the user explicitly asks, or after 2 consecutive regenerations of the SAME image that still miss the core request.
- Never analyze for minor tweaks (skin tone, color, sharpness, small details) or for issues that occurred on different images.

## Example

```
User: Draw me a red bicycle leaning against a brick wall

Agent: (generate_image with a rich prompt: "a red bicycle leaning against a
       weathered brick wall, soft morning light, photorealistic, shallow depth
       of field")
(send_signal_attachment with the returned filename)

Here you go!

User: Now make the bicycle blue

Agent: (generate_image with input_images=["generated_2025-01-01_12-00-00_abcd.png"] and
       prompt "make the bicycle blue, keep everything else the same")
(send_signal_attachment with the returned filename)

User: Combine the bicycle photo and the wall photo into one scene

Agent: (generate_image with input_images=["bicycle.png", "wall.png"] and
       prompt "combine these two images: the red bicycle leaning against the
       brick wall, seamless composition")
(send_signal_attachment with the returned filename)

User: Can you make this bigger?

Agent: (generate_image with input_images=["generated_2025-01-01_12-00-00_abcd.png"],
       size "2048x2048", prompt "Upscale this image so its longest edge is
       2048 px, keeping the aspect ratio. Keep all content, composition, and
       style exactly the same.")
       # no steps change - upscaling is a size change only, keep default passes
(send_signal_attachment with the returned filename)

User: Make this look better, it's a bit blurry

Agent: (generate_image with input_images=["generated_2025-01-01_12-00-00_abcd.png"],
       no size, prompt "Sharpen and add fine detail to this image, keeping
       content, composition, and style exactly the same.")
(send_signal_attachment with the returned filename)
```

## Notes

- Generation takes a while - the GPU is shared with the chat model. Set the
  user's expectations before calling, not after.
- To combine/merge multiple images, pass multiple filenames in `input_images`
  (up to 8 by default, configurable via `image_gen.max_input_images`). The
  model will blend elements from all reference images into the result.
- Keep prompts concrete: "watercolor painting of a fox in autumn woods"
  beats "a fox".
