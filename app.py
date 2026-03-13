import os
import json
import requests
from flask import Flask, request
from slack_bolt import App
from slack_bolt.adapter.flask import SlackRequestHandler
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

app = App(token=os.environ["SLACK_BOT_TOKEN"], signing_secret=os.environ["SLACK_SIGNING_SECRET"])
flask_app = Flask(__name__)
handler = SlackRequestHandler(app)

# Agent server URL
AGENT_SERVER_URL = os.environ.get("AGENT_SERVER_URL", "http://localhost:8000")

# Store conversation state per user/channel
conversation_state = {}

# Helper function to get project_id by channel_id
def get_project_by_channel(channel_id):
    """
    Get project_id associated with a Slack channel_id
    Returns project_id or None if not found
    """
    try:
        response = requests.get(
            f"{AGENT_SERVER_URL}/project-by-conversation/{channel_id}",
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        if response.status_code == 404:
            # No project linked to this channel
            return None
        
        response.raise_for_status()
        data = response.json()
        return data.get("project_id")
    except requests.exceptions.RequestException as e:
        print(f"Error fetching project_id for channel {channel_id}: {e}")
        return None

# Helper function to get Slack message history
def get_channel_message_history(channel_id, limit=50):
    """
    Retrieve previous messages from a Slack channel
    Returns list of messages with user, text, and timestamp
    """
    try:
        result = app.client.conversations_history(
            channel=channel_id,
            limit=limit
        )
        
        messages = []
        if result.get("ok"):
            for msg in result.get("messages", []):
                # Skip bot messages and system messages
                if msg.get("subtype") or msg.get("bot_id"):
                    continue
                
                messages.append({
                    "user_id": msg.get("user"),
                    "text": msg.get("text", ""),
                    "timestamp": msg.get("ts", "")
                })
        
        # Reverse to get chronological order (oldest first)
        return list(reversed(messages))
    except Exception as e:
        print(f"Error fetching message history for channel {channel_id}: {e}")
        return []

# Helper function to call the agent server
def call_agent_api(endpoint, data):
    try:
        for key, value in data.items():
            print(f"  {key}: {type(value).__name__} = {value if not isinstance(value, list) else f'list[{len(value)} items]'}")
        response = requests.post(f"{AGENT_SERVER_URL}/{endpoint}", json=data, timeout=30)
        response.raise_for_status()
        result = response.json()

        # Check if the API returned an error in the response body
        if "error" in result:
            raise Exception(f"API Error: {result['error']}")
        
        return result
    except requests.exceptions.RequestException as e:
        print(f"Error calling agent API {endpoint}: {e}")
        raise Exception(f"Failed to call agent API: {str(e)}")

# Helper function to split text into chunks for Slack blocks
def split_text_for_slack(text, max_length=2900):
    """
    Split text into chunks that fit within Slack's 3000 character limit
    Uses 2900 to leave room for formatting
    """
    if len(text) <= max_length:
        return [text]
    
    chunks = []
    current_chunk = ""
    
    # Split by lines to avoid breaking mid-sentence
    lines = text.split('\n')
    
    for line in lines:
        # If adding this line would exceed the limit
        if len(current_chunk) + len(line) + 1 > max_length:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = line + '\n'
            else:
                # Single line is too long, split it
                while len(line) > max_length:
                    chunks.append(line[:max_length])
                    line = line[max_length:]
                current_chunk = line + '\n'
        else:
            current_chunk += line + '\n'
    
    # Add the last chunk
    if current_chunk.strip():
        chunks.append(current_chunk.strip())
    
    return chunks

# Helper function to get latest proposal for a project
def get_latest_proposal(project_id):
    """
    Get the latest proposal for a project
    Returns proposal data or error
    """
    try:
        response = requests.get(
            f"{AGENT_SERVER_URL}/proposal/{project_id}",
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        if response.status_code == 404:
            return {"error": "No proposals found for this project."}
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching proposal for project {project_id}: {e}")
        return {"error": str(e)}

# Handle /ask command
@app.command("/ask")
def handle_ask_command(ack, say, command):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    query = command.get("text", "").strip()
    
    if not query:
        say("❌ Please provide a question. Usage: `/ask <your question>`")
        return
    
    # Show thinking indicator
    say(f"🤔 Processing your question: _{query}_\n⏳ Please wait...")
    
    # Get project_id for this channel
    project_id = get_project_by_channel(channel_id)
    
    # Get message history from this channel
    message_history = get_channel_message_history(channel_id, limit=50)
    
  
    try:
        # Convert message_history to list of strings (just the text)
        messages_text = [msg['text'] for msg in message_history]
        
        result = call_agent_api("chat", {
            "question": query,
            # "user_id": user_id,
            # "channel_id": channel_id,
            "project_id": project_id,
            "messages": messages_text
        })
    except Exception as e:
        say(f"❌ Error: {str(e)}")
        return
    
    # Store conversation state
    conversation_key = f"{user_id}_{channel_id}"
    conversation_state[conversation_key] = {
        "last_query": query,
        "last_response": result.get("answer", ""),
        "session_id": result.get("session_id", ""),
        "project_id": project_id,
        "timestamp": datetime.now().isoformat()
    }
    
    # Format and send response
    response_text = result.get("answer", "No response received")
    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Question:* {query}"
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Response:*\n{response_text}"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 Use `/regenerate` to get a different response or `/finalize` to confirm"
                    }
                ]
            }
        ]
    })

# Handle /regenerate command
@app.command("/regenerate")
def handle_regenerate_command(ack, say, command):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    conversation_key = f"{user_id}_{channel_id}"
    
    # Check if there's a previous conversation
    if conversation_key not in conversation_state:
        say("❌ No previous question found. Please use `/ask` first.")
        return
    
    state = conversation_state[conversation_key]
    original_query = state["last_query"]
    
    say(f"🔄 Regenerating response for: _{original_query}_\n⏳ Please wait...")
    
    # Get message history from this channel
    message_history = get_channel_message_history(channel_id, limit=50)
    
    # MOCK RESPONSE - Comment out for production
    result = {
        "response": f"This is a REGENERATED mock response to: '{original_query}'\n\n"
                   f"• Channel ID: {channel_id}\n"
                   f"• Project ID: {'Not linked'}\n"
                   f"• Previous messages: {len(message_history)} found\n"
                   f"• Session ID: {state.get('session_id', 'N/A')}\n\n"
                   f"🔄 This is a different version of the response (mock).",
        "session_id": state.get("session_id", "")
    }
    
    # Uncomment below for production (and comment out mock response above)
    # try:
    #     # Convert message_history to list of strings (just the text)
    #     messages_text = [msg['text'] for msg in message_history]
    #     
    #     result = call_agent_api("regenerate", {
    #         "query": original_query,
    #         "user_id": user_id,
    #         "channel_id": channel_id,
    #         "session_id": state.get("session_id", ""),
    #         "project_id": state.get("project_id"),
    #         "messages": messages_text
    #     })
    # except Exception as e:
    #     say(f"❌ Error: {str(e)}")
    #     return
    
    # Update conversation state
    conversation_state[conversation_key]["last_response"] = result.get("response", "")
    conversation_state[conversation_key]["timestamp"] = datetime.now().isoformat()
    
    # Format and send response
    response_text = result.get("response", "No response received")
    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Regenerated Response:*\n{response_text}"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 Use `/regenerate` again for another version or `/finalize` to confirm"
                    }
                ]
            }
        ]
    })

# Handle /finalize command
@app.command("/finalize")
def handle_finalize_command(ack, say, command):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    conversation_key = f"{user_id}_{channel_id}"
    
    # Check if there's a previous conversation
    if conversation_key not in conversation_state:
        say("❌ No conversation found. Please use `/ask` first.")
        return
    
    state = conversation_state[conversation_key]
    
    # MOCK RESPONSE - Comment out for production
    result = {
        "status": "finalized",
        "message": "Response has been finalized successfully"
    }
    
    # Uncomment below for production (and comment out mock response above)
    # try:
    #     result = call_agent_api("finalize", {
    #         "user_id": user_id,
    #         "channel_id": channel_id,
    #         "session_id": state.get("session_id", ""),
    #         "final_response": state["last_response"]
    #     })
    # except Exception as e:
    #     say(f"❌ Error: {str(e)}")
    #     return
    
    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "✅ *Response finalized!*"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Final Answer:*\n{state['last_response']}"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Session ID: `{state.get('session_id', 'N/A')}` | Timestamp: {state['timestamp']}"
                    }
                ]
            }
        ]
    })
    
    # Clear conversation state for this user/channel
    del conversation_state[conversation_key]

# Handle /proposal command
@app.command("/proposal")
def handle_proposal_command(ack, say, command):
    ack()
    channel_id = command["channel_id"]
    
    # Show loading indicator
    say("📄 Fetching proposal for this channel...\n⏳ Please wait...")
    
    # Get project_id for this channel
    project_id = get_project_by_channel(channel_id)
    
    if not project_id:
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "❌ *No Project Found*\n\nThis channel is not linked to any project. Please link a project to this channel first."
                    }
                }
            ]
        })
        return
    
    # # MOCK RESPONSE - Comment out for production
    # from datetime import datetime
    # proposal_data = {
    #     "project_id": project_id,
    #     "version": 2,
    #     "content": "# Project Proposal - Mock Data\n\n"
    #               "## Executive Summary\n"
    #               "This is a mock proposal for testing purposes.\n\n"
    #               "## Scope\n"
    #               "- Develop web application\n"
    #               "- Implement user authentication\n"
    #               "- Deploy to production\n\n"
    #               "## Budget\n"
    #               "$50,000\n\n"
    #               "## Timeline\n"
    #               "3 months\n\n"
    #               "✨ This is mock proposal data.",
    #     "timestamp": datetime.now().isoformat()
    # }
    
    # Uncomment below for production (and comment out mock response above)
    proposal_data = get_latest_proposal(project_id)
    
    if "error" in proposal_data:
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"❌ *Error*\n\n{proposal_data['error']}"
                    }
                }
            ]
        })
        return
    
    # Split proposal content into chunks if needed
    content = proposal_data['content']
    content_chunks = split_text_for_slack(content, max_length=2900)
    
    # Build blocks dynamically
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📄 Project Proposal"
            }
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Project ID:*\n`{proposal_data['project_id']}`"
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Version:*\n{proposal_data['version']}"
                }
            ]
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Last Updated:*\n{proposal_data['timestamp']}"
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Channel ID:*\n`{channel_id}`"
                }
            ]
        },
        {
            "type": "divider"
        }
    ]
    
    # Add content blocks
    for i, chunk in enumerate(content_chunks):
        if i == 0:
            # First chunk with header
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Proposal Content:*\n\n{chunk}"
                }
            })
        else:
            # Continuation chunks
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": chunk
                }
            })
    
    # Add footer
    blocks.append({
        "type": "context",
        "elements": [
            {
                "type": "mrkdwn",
                "text": f"💡 Use `/ask` to ask questions about this proposal | Content split into {len(content_chunks)} part(s)"
            }
        ]
    })
    
    # Format and send proposal
    say({"blocks": blocks})

# Handle /help command
@app.command("/help")
def handle_help_command(ack, say):
    ack()
    say({
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🤖 GammaBot Commands"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Available Commands:*"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "• `/ask <question>` - Ask the AI agent a question\n• `/proposal` - View the proposal for this channel\n• `/regenerate` - Generate a different response to your last question\n• `/finalize` - Confirm and save the current response\n• `/status` - Check bot status\n• `/clear` - Clear your conversation history\n• `/help` - Show this help message"
                }
            },
            {
                "type": "divider"
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💡 *Example:* `/ask What is machine learning?`"
                    }
                ]
            }
        ]
    })

# Handle /status command
@app.command("/status")
def handle_status_command(ack, say):
    ack()
    
    # Check agent server status
    try:
        response = requests.get(f"{AGENT_SERVER_URL}/health", timeout=5)
        agent_status = "🟢 Online" if response.status_code == 200 else "🟡 Limited"
    except:
        agent_status = "🔴 Offline"
    
    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*GammaBot Status*\n\n• Bot: 🟢 Online\n• Agent Server: {agent_status}\n• Active Sessions: {len(conversation_state)}"
                }
            }
        ]
    })

# Handle /clear command
@app.command("/clear")
def handle_clear_command(ack, say, command):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    conversation_key = f"{user_id}_{channel_id}"
    
    if conversation_key in conversation_state:
        del conversation_state[conversation_key]
        say("✅ Your conversation history has been cleared.")
    else:
        say("ℹ️ No conversation history found.")

# Handle app mentions in channels
@app.event("app_mention")
def handle_app_mention(event, say):
    text = event.get("text", "").lower()
    user_id = event["user"]
    channel_id = event["channel"]

    print("user_id", user_id)
    
    # Remove bot mention from text
    text = text.split(maxsplit=1)
    if len(text) > 1:
        query = text[1].strip()
    else:
        say(f"Hi <@{user_id}>! 👋\n\nUse `/help` to see available commands or mention me with a question!")
        return
    
    # Treat mentions as ask commands
    if query:
        say(f"🤔 Processing your question: _{query}_\n⏳ Please wait...")
        
        # Get project_id and message history
        project_id = get_project_by_channel(channel_id)
        message_history = get_channel_message_history(channel_id, limit=50)
        
        # MOCK RESPONSE - Comment out for production
        result = {
            "response": f"This is a mock response to your question: '{query}'\n\n"
                       f"• Channel ID: {channel_id}\n"
                       f"• Project ID: {project_id or 'Not linked'}\n"
                       f"• Previous messages: {len(message_history)} found\n\n"
                       f"✨ This is a test response. The actual agent API is not being called.",
            "session_id": f"mock_session_{channel_id}_{datetime.now().timestamp()}"
        }
        
        # Uncomment below for production (and comment out mock response above)
        # try:
        #     # Convert message_history to list of strings (just the text)
        #     messages_text = [msg['text'] for msg in message_history]
        #     
        #     result = call_agent_api("chat", {
        #         "question": query,
        #         "user_id": user_id,
        #         "channel_id": channel_id,
        #         "project_id": project_id,
        #         "messages": messages_text
        #     })
        # except Exception as e:
        #     say(f"❌ Error: {str(e)}")
        #     return
        
        response_text = result.get("response", "No response received")
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Response:*\n{response_text}"
                    }
                }
            ]
        })

# Handle direct messages (DMs) to the bot
@app.event("message")
def handle_message_events(event, say):
    if event.get("subtype") or event.get("bot_id"):
        return
    
    text = event.get("text", "").strip()
    user_id = event["user"]
    channel_id = event["channel"]

    print("user_id", user_id)
    print("channel_id", channel_id)
    
    if text.lower() in ["help", "hello", "hi"]:
        say(f"Hi <@{user_id}>! 👋\n\nI'm GammaBot, your AI assistant.\n\nUse `/help` to see all available commands or just type your question!")
    else:
        # Treat DM as ask command
        say(f"🤔 Processing your question...\n⏳ Please wait...")
        
        # Get project_id and message history
        project_id = get_project_by_channel(channel_id)
        message_history = get_channel_message_history(channel_id, limit=50)
        
        # MOCK RESPONSE - Comment out for production
        result = {
            "response": f"This is a mock response to your question: '{text}'\n\n"
                       f"• Channel ID: {channel_id}\n"
                       f"• Project ID: {project_id or 'Not linked'}\n"
                       f"• Previous messages: {len(message_history)} found\n\n"
                       f"✨ This is a test response. The actual agent API is not being called.",
            "session_id": f"mock_session_{channel_id}_{datetime.now().timestamp()}"
        }
        
        # Uncomment below for production (and comment out mock response above)
        # try:
        #     # Convert message_history to list of strings (just the text)
        #     messages_text = [msg['text'] for msg in message_history]
        #     
        #     result = call_agent_api("chat", {
        #         "question": text,
        #         "user_id": user_id,
        #         "channel_id": channel_id,
        #         "project_id": project_id,
        #         "messages": messages_text
        #     })
        # except Exception as e:
        #     say(f"❌ Error: {str(e)}")
        #     return
        
        response_text = result.get("response", "No response received")
        conversation_key = f"{user_id}_{channel_id}"
        conversation_state[conversation_key] = {
            "last_query": text,
            "last_response": response_text,
            "session_id": result.get("session_id", ""),
            "project_id": project_id,
            "timestamp": datetime.now().isoformat()
        }
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Response:*\n{response_text}"
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "💡 Use `/regenerate` for another version or `/finalize` to confirm"
                        }
                    ]
                }
            ]
        })

@flask_app.route("/slack", methods=["POST"])
def slack_handler():
    return handler.handle(request)

@flask_app.route("/health", methods=["GET"])
def health_check():
    return {"status": "healthy", "bot": "GammaBot"}, 200

if __name__ == "__main__":
    flask_app.run(debug=True, port=3000)
