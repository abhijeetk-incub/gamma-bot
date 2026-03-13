# GammaBot - AI-Powered Slack Bot

An intelligent Slack bot that integrates with an AI agent server to provide smart question-answering capabilities with regeneration and finalization features.

## Features

- **Ask Questions**: Use `/ask` to query the AI agent
- **Regenerate Responses**: Use `/regenerate` to get alternative answers
- **Finalize**: Use `/finalize` to confirm and save responses
- **Conversation History**: Maintains context per user/channel
- **Direct Messages**: Chat directly with the bot in DMs
- **App Mentions**: Mention the bot in channels to ask questions

## Commands

| Command | Description | Example |
|---------|-------------|---------|
| `/ask <question>` | Ask the AI agent a question | `/ask What is machine learning?` |
| `/regenerate` | Generate a different response to your last question | `/regenerate` |
| `/finalize` | Confirm and save the current response | `/finalize` |
| `/status` | Check bot and agent server status | `/status` |
| `/clear` | Clear your conversation history | `/clear` |
| `/help` | Show available commands | `/help` |

## Setup Instructions

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Update the `.env` file with your Slack credentials and agent server URL:

```env
SLACK_BOT_TOKEN=xoxb-your-token-here
SLACK_SIGNING_SECRET=your-signing-secret-here
AGENT_SERVER_URL=https://your-agent-server.com
```

### 3. Set Up Slack App

1. Go to [Slack API](https://api.slack.com/apps)
2. Create a new app or select your existing app
3. Add the following **Bot Token Scopes** under OAuth & Permissions:
   - `app_mentions:read`
   - `chat:write`
   - `commands`
   - `im:history`
   - `im:read`
   - `im:write`

4. Create the following **Slash Commands**:
   - `/ask` - Description: "Ask the AI agent a question"
   - `/regenerate` - Description: "Regenerate the last response"
   - `/finalize` - Description: "Finalize and save the current response"
   - `/status` - Description: "Check bot status"
   - `/clear` - Description: "Clear conversation history"
   - `/help` - Description: "Show help information"

   Set the **Request URL** for all commands to: `https://your-bot-url.com/slack`

5. Enable **Event Subscriptions**:
   - Request URL: `https://your-bot-url.com/slack`
   - Subscribe to bot events:
     - `app_mention`
     - `message.im`

6. Install the app to your workspace and copy the **Bot User OAuth Token** and **Signing Secret** to your `.env` file

### 4. Agent Server Requirements

Your agent server should implement the following endpoints:

#### POST `/ask`
Request body:
```json
{
  "query": "string",
  "user_id": "string",
  "channel_id": "string"
}
```

Response:
```json
{
  "response": "string",
  "session_id": "string"
}
```

#### POST `/regenerate`
Request body:
```json
{
  "query": "string",
  "user_id": "string",
  "channel_id": "string",
  "session_id": "string"
}
```

Response:
```json
{
  "response": "string"
}
```

#### POST `/finalize`
Request body:
```json
{
  "user_id": "string",
  "channel_id": "string",
  "session_id": "string",
  "final_response": "string"
}
```

Response:
```json
{
  "status": "success"
}
```

#### GET `/health`
Response:
```json
{
  "status": "healthy"
}
```

### 5. Run the Bot

```bash
python app.py
```

The bot will start on `http://localhost:3000`

### 6. Expose Locally (For Development)

Use ngrok or a similar tool to expose your local server:

```bash
ngrok http 3000
```

Update your Slack app's Request URLs with the ngrok URL.

## Usage Examples

### Ask a Question
```
/ask What is the difference between supervised and unsupervised learning?
```

The bot will:
1. Show a thinking indicator
2. Query the agent server
3. Display the response with formatting
4. Store the conversation state

### Regenerate Response
```
/regenerate
```

The bot will:
1. Use your last question
2. Request a new response from the agent
3. Update the conversation state
4. Display the new response

### Finalize Response
```
/finalize
```

The bot will:
1. Confirm the current response
2. Send it to the agent server for storage
3. Clear the conversation state
4. Show a confirmation message

### Direct Messages
You can also send direct messages to the bot:
```
What are neural networks?
```

### App Mentions
Mention the bot in any channel:
```
@GammaBot explain quantum computing
```

## Architecture

```
┌─────────────┐         ┌──────────────┐         ┌──────────────┐
│   Slack     │ ◄─────► │  GammaBot    │ ◄─────► │ Agent Server │
│   Users     │         │  (Flask App) │         │   (Your API) │
└─────────────┘         └──────────────┘         └──────────────┘
                               │
                               ▼
                        ┌──────────────┐
                        │ Conversation │
                        │    State     │
                        └──────────────┘
```

## Conversation Flow

1. **User asks a question** → Bot stores query and response
2. **User regenerates** → Bot requests new response, updates state
3. **User finalizes** → Bot saves to agent server, clears state

Each user/channel combination maintains independent conversation state.

## Error Handling

The bot handles various error scenarios:
- Agent server unavailable
- Network timeouts
- Invalid queries
- No conversation history

All errors are reported to the user with helpful messages.

## Development

### Project Structure
```
GammaSlackBot/
├── app.py              # Main bot application
├── requirements.txt    # Python dependencies
├── .env               # Environment variables
└── README.md          # This file
```

### Adding New Commands

To add a new command:

1. Add the command handler in `app.py`:
```python
@app.command("/yourcommand")
def handle_your_command(ack, say, command):
    ack()
    # Your logic here
    say("Response")
```

2. Create the command in your Slack app settings
3. Restart the bot

## Troubleshooting

### Bot not responding
- Check that the bot is running: `python app.py`
- Verify your Slack credentials in `.env`
- Ensure the Request URL is correct in Slack app settings
- Check ngrok is running (for local development)

### Agent server errors
- Verify `AGENT_SERVER_URL` is correct in `.env`
- Check that the agent server is running
- Test agent server endpoints manually

### Command not found
- Ensure commands are created in Slack app settings
- Verify the Request URL is set for each command
- Reinstall the app to your workspace

## License

MIT License
