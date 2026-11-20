import re
from pathlib import Path
from typing import Dict, Any
from Skills.youtube_skill import YouTubeSkill
from Skills.video_generation_skill import VideoGenerationSkill

class BuddyAgent:
    def __init__(self, client, model: str):
        self.client = client
        self.model = model
        self.youtube_skill = YouTubeSkill()
        self.video_skill = VideoGenerationSkill()

    async def handle_message(self, user_input: str) -> str:
        text_lower = user_input.lower()
        
        # 1. Check for YouTube Link
        youtube_pattern = r'(https?://(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)\S+)'
        match = re.search(youtube_pattern, user_input)
        if match:
            url = match.group(1)
            try:
                data = await self.youtube_skill.execute(url)
                return (
                    f"?? **YouTube Video Details (Analyzed by Buddy):**\n"
                    f"- **Title:** {data['title']}\n"
                    f"- **Channel:** {data['channel']}\n"
                    f"- **Views:** {data.get('view_count', 'N/A')}\n"
                    f"- **Likes:** {data.get('like_count', 'N/A')}"
                )
            except Exception as e:
                return f"? Failed to fetch video details: {e}"

        # 2. Check for Video Generation Request
        if any(keyword in text_lower for keyword in ["video bana", "3d video", "generate video", "render video"]):
            try:
                # Default 1-minute cinematic lab generation
                path = await self.video_skill.execute(scenario=user_input, duration=60, fps=24)
                return f"? **3D Video Successfully Generated & Saved!**\n?? Path: {path}"
            except Exception as e:
                return f"? Video generation failed: {e}"

        # 3. Default AI response fallback
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": user_input}],
            temperature=0.7
        )
        return resp.choices[0].message.content
