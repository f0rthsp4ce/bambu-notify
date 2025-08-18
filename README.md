# Bambu Notify

**Smart monitoring and notifications for your Bambu 3D printers**

Never miss a print failure again! Bambu Notify is a comprehensive monitoring solution that watches your Bambu 3D printers and keeps you informed about everything happening with your prints - whether you're at home or away.

## 🎯 What it does

**Real-time Printer Monitoring**
- Connects to one or multiple Bambu printers via WebSocket for live status updates
- Tracks print progress, temperatures, speeds, and error states
- Automatically discovers and monitors all printers on your network

**Smart Telegram Notifications**
- 📱 Print start/stop notifications with job details  
- 📊 Progress updates at configurable intervals (every 5%, 10%, etc.)
- 🚨 Instant alerts for print errors or failures
- 📸 Hourly photo snapshots during printing
- 🏁 Final completion photo and print summary
- 🎬 Automatic timelapse video creation and delivery

**AI-Powered Failure Detection** *(Optional)*
- Uses advanced AI vision models to detect print failures
- Analyzes camera frames for issues like spaghetti, layer shifts, adhesion problems
- Sends immediate alerts with evidence photos when problems are detected

**Professional Monitoring & APIs**
- Prometheus metrics for integration with Grafana dashboards
- REST APIs to access printer status and camera feeds
- Automatic image archival with configurable retention
- Supports multiple printers with unified monitoring

## 🤔 Why you need this

**Peace of Mind**
- Get notified the moment something goes wrong, not hours later
- Monitor long prints remotely without constantly checking your phone
- Sleep soundly knowing you'll be alerted if your overnight print fails

**Never Waste Filament Again**
- AI failure detection catches problems early, saving materials and time
- Progress notifications help you plan when to return to collect finished prints
- Error alerts prevent you from discovering failed prints too late

**Professional Print Farm Management**
- Monitor multiple printers from a single dashboard
- Historical data and metrics for optimizing your printing workflow
- Automated timelapse creation for sharing your successful prints

## 🚀 Quick Start

### Prerequisites

1. **Bambu printer** with network connectivity
2. **Telegram account** for notifications
3. **Python 3.8+** or **Docker**

### 1. Set up Telegram Bot

**Create a bot:**
1. Message `@BotFather` on Telegram
2. Send `/newbot` and follow instructions to create your bot
3. Save the bot token (looks like `123456789:ABCDEF...`)

**Get your Chat ID:**
1. Add your bot to a group/chat where you want notifications
2. Message `@RawDataBot` in that chat to get the chat ID
3. Save the chat ID (looks like `-1001234567890` for groups)

*Optional: For topic-based groups, you can also set a specific thread ID*

### 2. Choose Your Installation Method

#### Option A: Docker (Recommended)

```bash
# Create and run the container
docker run -d --name bambu-notify \
  -p 8000:8000 \
  -e TELEGRAM_BOT_TOKEN="YOUR_BOT_TOKEN" \
  -e TELEGRAM_CHAT_ID="YOUR_CHAT_ID" \
  -e PRINTER_ID="YOUR_PRINTER_ID" \
  -v $(pwd)/images:/app/images \
  --restart unless-stopped \
  bambu-notify:latest
```

#### Option B: Python Installation

```bash
# Clone and setup
git clone https://github.com/yourusername/bambu-notify.git
cd bambu-notify
python3 -m venv .venv && source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables
export TELEGRAM_BOT_TOKEN="YOUR_BOT_TOKEN"
export TELEGRAM_CHAT_ID="YOUR_CHAT_ID"
export PRINTER_ID="YOUR_PRINTER_ID"

# Run the application
python app.py
```

### 3. Verify Setup

1. Check the service is running: `curl http://localhost:8000/api/`
2. View your printer status: `curl http://localhost:8000/api/printer/YOUR_PRINTER_ID`
3. Start a print job and wait for notifications!

## ⚙️ Configuration

### Essential Settings

These are the minimum settings needed to get started:

| Variable | Required | Description | Example |
|----------|----------|-------------|---------|
| `TELEGRAM_BOT_TOKEN` | **Yes** | Your Telegram bot token from @BotFather | `123456789:ABCDEF...` |
| `TELEGRAM_CHAT_ID` | **Yes** | Chat ID where notifications will be sent | `-1001234567890` |
| `PRINTER_ID` | No | Your printer ID (auto-discovered if not set) | `A1M-1` |

### Notification Settings

Customize when and how you receive notifications:

| Variable | Default | Description |
|----------|---------|-------------|
| `TELEGRAM_THREAD_ID` | — | Send to specific topic/thread (optional) |
| `PROGRESS_STEP` | `5` | Progress notification frequency (%) |
| `PHOTO_INTERVAL_SECONDS` | `3600` | How often to send progress photos (seconds) |

### AI Failure Detection (Optional)

Enable smart failure detection with these settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | — | API key from OpenRouter.ai |
| `AI_MODEL` | `google/gemini-2.5-flash` | AI model for defect detection |
| `AI_CHECK_INTERVAL_SECONDS` | `3600` | How often to check for defects |
| `AI_CONFIDENCE_THRESHOLD` | `0.7` | Minimum confidence to trigger alerts (0-1) |

### Advanced Settings

For fine-tuning and multi-printer setups:

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8000` | HTTP port for the web interface |
| `PRINTERS_API_URL` | Auto | API endpoint for printer discovery |
| `PRINTERS_REFRESH_SECONDS` | `3600` | How often to refresh printer list |
| `IMAGES_DIR` | `images` | Directory to save camera snapshots |
| `IMAGE_RETENTION_DAYS` | `7` | How long to keep saved images |
| `TIMELAPSE_MAX_BYTES` | `50MB` | Maximum timelapse file size |
| `RECONNECT_MIN_SECONDS` | `1` | Minimum reconnection delay |
| `RECONNECT_MAX_SECONDS` | `30` | Maximum reconnection delay |

### Example Configuration

```bash
# Essential settings
export TELEGRAM_BOT_TOKEN="123456789:ABCDEF..."
export TELEGRAM_CHAT_ID="-1001234567890"

# Optional: Enable AI failure detection
export OPENROUTER_API_KEY="sk-or-..."

# Optional: Customize notifications
export PROGRESS_STEP="10"  # Notify every 10% instead of 5%
export PHOTO_INTERVAL_SECONDS="1800"  # Send photos every 30 minutes
```

## 📡 API Reference

Bambu Notify provides REST APIs for integrating with other tools or building custom dashboards.

### Available Endpoints

| Endpoint | Method | Description | Response |
|----------|--------|-------------|----------|
| `/api/` | GET | Health check | `"OK"` |
| `/api/printers` | GET | List all discovered printers | JSON array |
| `/api/printer/{id}` | GET | Get latest status for specific printer | JSON object |
| `/api/printer/{id}/image` | GET | Get latest camera frame | JPEG image |
| `/metrics` | GET | Prometheus metrics | Text format |

### Example API Usage

```bash
# Check if service is running
curl http://localhost:8000/api/

# List all printers
curl http://localhost:8000/api/printers | jq

# Get printer status
curl http://localhost:8000/api/printer/A1M-1 | jq

# Download latest camera frame
curl -o current_frame.jpg http://localhost:8000/api/printer/A1M-1/image
```

### Sample Response

```json
{
  "printer_id": "A1M-1",
  "received_at": "2024-01-15T10:30:00Z",
  "status": {
    "gcode_state": "RUNNING",
    "mc_percent": 45,
    "mc_remaining_time": 120,
    "subtask_name": "awesome_print.3mf",
    "layer_num": 89,
    "total_layer_num": 200,
    "nozzle_temper": 220,
    "bed_temper": 60
  }
}
```

## 📊 Monitoring & Dashboards

### Prometheus Metrics

Bambu Notify exports comprehensive metrics for monitoring with Prometheus and Grafana.

**Key Metrics:**
- `bambu_printer_up{printer_id}` - Printer online status
- `bambu_printer_is_active{printer_id}` - Active print detection
- `bambu_progress_percent{printer_id}` - Print progress percentage
- `bambu_remaining_time_minutes{printer_id}` - Estimated time remaining
- `bambu_nozzle_temperature_celsius{printer_id}` - Current nozzle temperature
- `bambu_bed_temperature_celsius{printer_id}` - Current bed temperature
- And many more...

**Prometheus Configuration:**
```yaml
scrape_configs:
  - job_name: bambu_notify
    static_configs:
      - targets: ["localhost:8000"]
    scrape_interval: 30s
```

### Grafana Dashboard

A pre-built Grafana dashboard is included (`dashboard.json`) with:
- 📈 Real-time progress tracking
- 🌡️ Temperature monitoring (nozzle, bed, chamber)
- 🖥️ System health metrics
- 📊 Print statistics and history
- 🏃 Speed and fan monitoring

**To import the dashboard:**
1. Open Grafana → Dashboards → Import
2. Upload `dashboard.json` from this repository
3. Select your Prometheus data source
4. Enjoy your new monitoring dashboard!

## 🎥 Advanced Features

### Automatic Timelapse Creation

Bambu Notify automatically creates timelapse videos when prints complete:

- **Automatic capture:** Photos are saved during active prints
- **Smart compilation:** Uses ffmpeg to create smooth timelapses 
- **Size optimization:** Videos are kept under 50MB for easy sharing
- **Telegram delivery:** Completed timelapses are automatically sent to your chat

**How it works:**
1. During printing, snapshots are saved to `images/{printer_id}/{job_name}/`
2. When the print finishes, ffmpeg compiles all frames into an MP4
3. The timelapse is automatically sent to your Telegram chat
4. Original frames are retained for 7 days (configurable)

### Manual Timelapse Creation

Want to create custom timelapses? You can use the saved frames directly:

```bash
# Navigate to your job's image folder
cd "images/A1M-1/my_awesome_print"

# Create a 30-second timelapse at 30 FPS
ffmpeg -pattern_type glob -i '*.jpg' \
  -vf "scale=1280:-2" -r 30 -t 30 \
  -c:v libx264 -pix_fmt yuv420p \
  my_custom_timelapse.mp4
```

### AI Failure Detection

Enable intelligent print monitoring with AI-powered defect detection:

**Setup:**
1. Get an API key from [OpenRouter.ai](https://openrouter.ai)
2. Set `OPENROUTER_API_KEY` in your environment
3. Bambu Notify will automatically analyze frames every hour

**What it detects:**
- 🍝 Spaghetti failures
- 📐 Layer shifts and misalignments  
- 🔗 Adhesion problems
- 💥 Nozzle crashes
- 🔄 Severe stringing

**Smart alerts:**
- Only triggers on high-confidence detections
- Sends evidence photos along with alerts
- Mentions you directly for urgent issues
- Ignores camera blur/focus issues

**Supported Models:**
- `google/gemini-2.5-flash` (default, fast and accurate)
- `anthropic/claude-3.5-sonnet` (highly accurate)
- `openai/gpt-4o` (reliable detection)

## 🔧 Troubleshooting

### Common Issues

**No notifications received:**
1. Verify your bot token and chat ID are correct
2. Ensure the bot has permission to post in your chat/group
3. Check if the bot is actually added to the correct chat
4. Look at the application logs for error messages

**Printer not detected:**
1. Verify your `PRINTER_ID` matches your actual printer ID
2. Check if your printer is on the same network
3. Ensure the printer discovery API is accessible
4. Try the debug endpoint: `/api/debug/state`

**Timelapse not created:**
1. Make sure `ffmpeg` is installed and accessible
2. Check if there are enough frames saved (minimum 2 required)
3. Verify disk space for saving videos
4. Look for ffmpeg error messages in logs

**AI detection not working:**
1. Verify your OpenRouter API key is valid and has credits
2. Check the AI model is supported and available
3. Ensure internet connectivity for API calls
4. Verify the confidence threshold isn't set too high

### Debug Endpoints

Use these endpoints to diagnose issues:

```bash
# Check overall system state
curl http://localhost:8000/api/debug/state | jq

# Force an immediate status update
curl -X POST http://localhost:8000/api/debug/force-seed
```

### Logs and Monitoring

**View application logs:**
```bash
# For Docker
docker logs bambu-notify

# For Python installation  
python app.py  # logs will show in terminal
```

**Health check:**
```bash
curl http://localhost:8000/api/
# Should return "OK"
```

## 💡 Tips & Best Practices

### Security
- Keep your bot token secure and never commit it to version control
- Consider using environment files or container secrets for sensitive data
- Use a reverse proxy (nginx/traefik) with TLS for production deployments

### Performance
- The application stores only the latest status and image in memory
- Historical data is available through Prometheus metrics
- Images are automatically cleaned up after the retention period
- Consider adjusting notification frequency for high-traffic setups

### Multi-Printer Setup
- Printers are automatically discovered from the configured API endpoint
- Each printer gets its own WebSocket connection and state tracking
- All notifications include the printer ID for easy identification
- Use Grafana dashboard variables to filter by specific printers

### Backup and Recovery
- Important data is stored in the `images/` directory
- Prometheus metrics can be backed up if historical data is needed
- Configuration is entirely through environment variables

## 🤝 Contributing

Found a bug or want to add a feature? Contributions are welcome!

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📝 License

MIT License - see LICENSE file for details.

---

**Happy printing!** 🎯 If Bambu Notify saves your prints or helps you catch failures early, consider starring the repository to show your support.
