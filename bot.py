import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# YT-DLP options
ydl_opts = {
    'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
    'outtmpl': 'downloads/%(id)s.%(ext)s',
    'merge_output_format': 'mp4',
    'prefer_ffmpeg': True,
    'keepvideo': True,
    'extract_flat': False,
    'quiet': True,
    'no_warnings': True,
    'extractor_args': {
        'instagram': {
            'direct': True,
        }
    }
}

async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text
    chat_id = update.message.chat_id
    
    await update.message.reply_text("📥 Processing your request...")
    
    try:
        # For Instagram direct URLs
        if 'instagram.com' in url and 'scontent' in url:
            import requests
            response = requests.get(url, stream=True)
            if response.status_code == 200:
                video_path = 'downloads/instagram_video.mp4'
                with open(video_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=1024*1024):
                        if chunk:
                            f.write(chunk)
                
                # Check file size and send
                file_size = os.path.getsize(video_path)
                if file_size > 50 * 1024 * 1024:
                    await update.message.reply_text("📤 File is large, sending as document...")
                    with open(video_path, 'rb') as video_file:
                        await context.bot.send_document(
                            chat_id=chat_id,
                            document=video_file
                        )
                else:
                    with open(video_path, 'rb') as video_file:
                        await context.bot.send_video(
                            chat_id=chat_id,
                            video=video_file,
                            supports_streaming=True
                        )
                os.remove(video_path)
                return
        
        # For regular URLs
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            video_path = ydl.prepare_filename(info)
            video_title = info.get('title', 'Video')
            
            # Extract full caption/description
            description = info.get('description', '')
            uploader = info.get('uploader', '')
            caption_text = f"🎥 {video_title}\n\n"
            
            if uploader:
                caption_text += f"👤 {uploader}\n\n"
            
            if description:
                caption_text += f"{description}"
            
            # Check file size
            file_size = os.path.getsize(video_path)
            
            # If file is larger than 50MB, send as document
            if file_size > 50 * 1024 * 1024:
                await update.message.reply_text("📤 File is large, sending as document to preserve quality...")
                with open(video_path, 'rb') as video_file:
                    await context.bot.send_document(
                        chat_id=chat_id,
                        document=video_file
                    )
            else:
                # Send as video for smaller files
                with open(video_path, 'rb') as video_file:
                    await context.bot.send_video(
                        chat_id=chat_id,
                        video=video_file,
                        supports_streaming=True
                    )
            
            # Send caption as separate message with full information
            await context.bot.send_message(
                chat_id=chat_id,
                text=caption_text,
                disable_web_page_preview=True
            )
            
            # Clean up
            os.remove(video_path)
            
    except Exception as e:
        await update.message.reply_text(f"❌ Sorry, couldn't download the video: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Hi! Send me a video link from YouTube, Twitter/X, Instagram, or TikTok, "
        "and I'll download it for you!"
    )

def main():
    # Create downloads directory if it doesn't exist
    os.makedirs("downloads", exist_ok=True)
    
    # Initialize bot with token from environment variable
    token = os.getenv('BOT_TOKEN')
    application = Application.builder().token(token).build()

    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    # Start the bot
    application.run_polling()

if __name__ == '__main__':
    main()