# Analyze Image

Use AI vision to analyze images and understand their contents.

## When to Use

**USE this skill when:**
- User sends an image and asks "what is this?"
- User asks about content in an image (text, objects, people)
- User wants details about a photo or picture
- User asks to "analyze this image"

**DON'T use this skill when:**
- User asks about historical images → use web_search
- User asks to generate an image → use the generate_image skill

## How to Use

1. First, list available images using file_list with path "attachments/"
2. Note the filename of the image
3. Use analyze_image tool with:
   - `image_filename`: the filename (e.g., "abc123.jpg")
   - `image_query`: what you want to know about the image

## Example

```
User: What's in this image?

Agent: Let me check the attachments folder for images.
(file_list to "attachments/")

I found an image. Let me analyze it.
(analyze_image with filename and query)
```

## Notes

- Images are stored in the attachments/ directory
- The analyze_image tool uses AI vision to understand image content
- Be specific in your query to get relevant information
