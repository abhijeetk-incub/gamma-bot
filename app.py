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
        # Increased timeout to 120 seconds for AI generation tasks
        response = requests.post(f"{AGENT_SERVER_URL}/{endpoint}", json=data, timeout=120)
        response.raise_for_status()
        result = response.json()

        # Check if the API returned an error in the response body
        if "error" in result:
            raise Exception(f"API Error: {result['error']}")
        
        return result
    except requests.exceptions.RequestException as e:
        print(f"Error calling agent API {endpoint}: {e}")
        raise Exception(f"Failed to call agent API: {str(e)}")

# Helper function to convert markdown to Slack mrkdwn format
def convert_markdown_to_slack(markdown_text):
    """
    Convert standard markdown to Slack's mrkdwn format for better readability
    """
    import re
    
    lines = markdown_text.split('\n')
    converted_lines = []
    in_code_block = False
    
    for line in lines:
        # Handle code blocks
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            converted_lines.append('```')
            continue
        
        if in_code_block:
            converted_lines.append(line)
            continue
        
        # Convert headers
        if line.startswith('# '):
            # Main header - larger, bold with emoji
            converted_lines.append(f"\n*{line[2:].strip()}*\n")
        elif line.startswith('## '):
            # Section header - bold with spacing
            converted_lines.append(f"\n*{line[3:].strip()}*")
        elif line.startswith('### '):
            # Subsection header
            converted_lines.append(f"\n*{line[4:].strip()}*")
        elif line.startswith('#### '):
            # Minor header
            converted_lines.append(f"\n_{line[5:].strip()}_")
        # Convert --- to a visual separator
        elif line.strip() == '---':
            converted_lines.append('\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n')
        else:
            # Convert **bold** to *bold*
            line = re.sub(r'\*\*(.+?)\*\*', r'*\1*', line)
            # Convert __italic__ to _italic_
            line = re.sub(r'__(.+?)__', r'_\1_', line)
            # Convert `code` to `code` (already good)
            # Handle lists - ensure proper spacing
            if line.strip().startswith('- ') or line.strip().startswith('* '):
                converted_lines.append(line)
            elif line.strip().startswith(('1.', '2.', '3.', '4.', '5.', '6.', '7.', '8.', '9.')):
                converted_lines.append(line)
            else:
                converted_lines.append(line)
    
    return '\n'.join(converted_lines)

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
        say("Please provide a question. Usage: `/ask <your question>`")
        return
    
    # Show thinking indicator
    say(f"Processing your question: _{query}_\nPlease wait...")
    
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
        say(f"Error: {str(e)}")
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
                        "text": "Use /refine-proposal to get a different response or /finalize to confirm"
                    }
                ]
            }
        ]
    })

# Handle /refine command (refine proposal)
@app.command("/refine-proposal")
def handle_refine_command(ack, say, command):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    
    # Get project_id for this channel
    project_id = get_project_by_channel(channel_id)
    
    if not project_id:
        say("This channel is not linked to any project. Please link a project first.")
        return
    
    # Get message history from this channel
    message_history = get_channel_message_history(channel_id, limit=50)
    
    # Collect all messages including /ask questions
    all_messages = []
    
    # Add regular channel messages
    all_messages.extend(message_history)
    
    # Check for /ask conversation history and include it
    ask_conversation_key = f"{user_id}_{channel_id}"
    if ask_conversation_key in conversation_state:
        ask_state = conversation_state[ask_conversation_key]
        # Add the question and response from /ask
        all_messages.append({
            "user_id": user_id,
            "text": f"Question: {ask_state['last_query']}",
            "timestamp": ask_state['timestamp']
        })
        all_messages.append({
            "user_id": "bot",
            "text": f"Response: {ask_state['last_response']}",
            "timestamp": ask_state['timestamp']
        })
    
    # Check if there was a previous refine that was rejected or needs continuation
    refine_history_key = f"refine_history_{channel_id}"
    if refine_history_key in conversation_state:
        prev_refine = conversation_state[refine_history_key]
        if prev_refine.get("status") == "rejected":
            # Include previous rejected messages
            say("Including messages from previous rejected refinement...")
            # Get timestamp of last refine to include only newer messages
            last_refine_time = prev_refine.get("timestamp")
            # Add a note about this
            all_messages.insert(0, {
                "user_id": "system",
                "text": "[Previous refinement was rejected - including those messages]",
                "timestamp": last_refine_time
            })
    
    if not all_messages:
        say("No conversation history found in this channel. Please use `/ask` first or send some messages.")
        return
    
    # Sort messages by timestamp
    all_messages.sort(key=lambda x: x.get('timestamp', ''))
    
    # Format messages for display (show last 10)
    messages_preview = ""
    display_messages = all_messages[-10:] if len(all_messages) > 10 else all_messages
    for i, msg in enumerate(display_messages, 1):
        user_info = f"<@{msg['user_id']}>" if msg.get('user_id') and msg['user_id'] not in ["bot", "system"] else ("GammaBot" if msg['user_id'] == "bot" else "System")
        text_preview = msg['text'][:100] + "..." if len(msg['text']) > 100 else msg['text']
        messages_preview += f"{i}. {user_info}: _{text_preview}_\n"
    
    # Store the messages for later use
    conversation_key = f"refine_{user_id}_{channel_id}"
    conversation_state[conversation_key] = {
        "messages": all_messages,
        "project_id": project_id,
        "timestamp": datetime.now().isoformat()
    }
    
    # Show preview with action buttons
    say({
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "Refine Proposal - Review Messages"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*These messages will be sent to the AI to refine the proposal:*\n\n{messages_preview}"
                }
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Total messages: {len(all_messages)} | Showing: Last {len(display_messages)}"
                    }
                ]
            },
            {
                "type": "divider"
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Choose an action:*"
                }
            },
            {
                "type": "actions",
                "block_id": f"refine_actions_{channel_id}",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Approve"
                        },
                        "style": "primary",
                        "value": f"{user_id}_{channel_id}",
                        "action_id": "refine_approve"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Reject"
                        },
                        "style": "danger",
                        "value": f"{user_id}_{channel_id}",
                        "action_id": "refine_reject"
                    },
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "Edit"
                        },
                        "value": f"{user_id}_{channel_id}",
                        "action_id": "refine_edit"
                    }
                ]
            }
        ]
    })

# Handle "Approve" button click
@app.action("refine_approve")
def handle_refine_approve(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    conversation_key = f"refine_{user_id}_{channel_id}"
    
    # Check if we have the stored messages
    if conversation_key not in conversation_state:
        say("Session expired. Please run `/refine-proposal` again.")
        return
    
    state = conversation_state[conversation_key]
    message_history = state["messages"]
    project_id = state["project_id"]
    
    # Update the message to show processing
    say(f"*Approved!* Refining proposal with {len(message_history)} messages...\nPlease wait...")
    
    try:
        # Convert message_history to list of strings (just the text)
        messages_text = [msg['text'] for msg in message_history]
        
        result = call_agent_api("generate", {
            "project_id": project_id,
            "messages": messages_text
        })
    except Exception as e:
        say(f"Error: {str(e)}")
        return
    
    # Format and send refined proposal
    # Convert markdown to Slack format and split into chunks if needed
    content = result.get("content", "No response received")
    slack_formatted_content = convert_markdown_to_slack(content)
    content_chunks = split_text_for_slack(slack_formatted_content, max_length=2800)
    
    # Build blocks dynamically
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Refined Proposal Generated"
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Project:* `{result.get('project_id')}` • *Version:* {result.get('version')} • *Updated:* {result.get('timestamp')}"
                }
            ]
        },
        {
            "type": "divider"
        }
    ]
    
    # Add content blocks
    for i, chunk in enumerate(content_chunks):
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": chunk
            }
        })
        
        # Add divider between chunks for better readability
        if i < len(content_chunks) - 1:
            blocks.append({"type": "divider"})
    
    # Add footer
    blocks.extend([
        {
            "type": "divider"
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Use /show-proposal to view the full proposal • /refine-proposal to refine again"
                }
            ]
        }
    ])
    
    say({"blocks": blocks})
    
    # Store refinement history for tracking
    refine_history_key = f"refine_history_{channel_id}"
    conversation_state[refine_history_key] = {
        "status": "approved",
        "messages": message_history,
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id
    }
    
    # Clean up the temporary conversation state
    del conversation_state[conversation_key]

# Handle "Reject" button click
@app.action("refine_reject")
def handle_refine_reject(ack, body, say):
    ack()
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    conversation_key = f"refine_{user_id}_{channel_id}"
    
    # Store rejection in history (DON'T delete - keep for next refinement)
    if conversation_key in conversation_state:
        state = conversation_state[conversation_key]
        
        # Store refinement history with rejected status
        refine_history_key = f"refine_history_{channel_id}"
        conversation_state[refine_history_key] = {
            "status": "rejected",
            "messages": state["messages"],
            "timestamp": datetime.now().isoformat(),
            "user_id": user_id
        }
        
        # Clean up the temporary conversation state
        del conversation_state[conversation_key]
    
    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Proposal refinement cancelled.*\n\nNo changes were made. These messages will be included if you run /refine-proposal again."
                }
            }
        ]
    })

# Handle "Edit" button click - Opens a modal
@app.action("refine_edit")
def handle_refine_edit(ack, body, client):
    ack()
    user_id = body["user"]["id"]
    channel_id = body["channel"]["id"]
    conversation_key = f"refine_{user_id}_{channel_id}"
    
    # Check if we have the stored messages
    if conversation_key not in conversation_state:
        client.chat_postMessage(
            channel=channel_id,
            text="Session expired. Please run `/refine-proposal` again."
        )
        return
    
    state = conversation_state[conversation_key]
    message_history = state["messages"]
    
    # Format messages for editing
    messages_text = "\n\n".join([
        f"Message {i+1} (from <@{msg['user_id']}>):\n{msg['text']}"
        for i, msg in enumerate(message_history[-10:])  # Last 10 messages
    ])
    
    # Open modal for editing
    client.views_open(
        trigger_id=body["trigger_id"],
        view={
            "type": "modal",
            "callback_id": "refine_edit_modal",
            "title": {
                "type": "plain_text",
                "text": "Edit Messages"
            },
            "submit": {
                "type": "plain_text",
                "text": "Submit"
            },
            "close": {
                "type": "plain_text",
                "text": "Cancel"
            },
            "private_metadata": f"{user_id}_{channel_id}",
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*Edit the messages below or add additional context:*"
                    }
                },
                {
                    "type": "input",
                    "block_id": "edited_messages",
                    "label": {
                        "type": "plain_text",
                        "text": "Messages to send to AI"
                    },
                    "element": {
                        "type": "plain_text_input",
                        "action_id": "messages_input",
                        "multiline": True,
                        "initial_value": messages_text,
                        "placeholder": {
                            "type": "plain_text",
                            "text": "Edit the messages or add additional instructions..."
                        }
                    }
                }
            ]
        }
    )

# Handle modal submission for edited messages
@app.view("refine_edit_modal")
def handle_refine_edit_modal_submission(ack, body, client, view):
    ack()
    
    # Extract the edited messages
    metadata = view["private_metadata"]
    user_id, channel_id = metadata.split("_")
    conversation_key = f"refine_{user_id}_{channel_id}"
    
    edited_text = view["state"]["values"]["edited_messages"]["messages_input"]["value"]
    
    # Check if we still have the state
    if conversation_key not in conversation_state:
        client.chat_postMessage(
            channel=channel_id,
            text="Session expired. Please run `/refine-proposal` again."
        )
        return
    
    state = conversation_state[conversation_key]
    project_id = state["project_id"]
    
    # Send processing message
    client.chat_postMessage(
        channel=channel_id,
        text=f"*Edited messages received!* Refining proposal...\nPlease wait..."
    )
    
    try:
        # Split edited text into messages (simple split by double newline)
        edited_messages = [msg.strip() for msg in edited_text.split("\n\n") if msg.strip()]
        
        result = call_agent_api("generate", {
            "project_id": project_id,
            "messages": edited_messages
        })
    except Exception as e:
        client.chat_postMessage(
            channel=channel_id,
            text=f"Error: {str(e)}"
        )
        return
    
    # Format and send refined proposal
    # Convert markdown to Slack format and split into chunks if needed
    content = result.get("content", "No response received")
    slack_formatted_content = convert_markdown_to_slack(content)
    content_chunks = split_text_for_slack(slack_formatted_content, max_length=2800)
    
    # Build blocks dynamically
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Refined Proposal Generated (Edited)"
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Project:* `{result.get('project_id')}` • *Version:* {result.get('version')} • *Updated:* {result.get('timestamp')}"
                }
            ]
        },
        {
            "type": "divider"
        }
    ]
    
    # Add content blocks
    for i, chunk in enumerate(content_chunks):
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": chunk
            }
        })
        
        # Add divider between chunks for better readability
        if i < len(content_chunks) - 1:
            blocks.append({"type": "divider"})
    
    # Add footer
    blocks.extend([
        {
            "type": "divider"
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Use /show-proposal to view the full proposal • /refine-proposal to refine again"
                }
            ]
        }
    ])
    
    client.chat_postMessage(
        channel=channel_id,
        blocks=blocks
    )
    
    # Store refinement history for tracking (edited version)
    refine_history_key = f"refine_history_{channel_id}"
    # Create messages list from edited text
    edited_messages_list = [{"user_id": user_id, "text": msg, "timestamp": datetime.now().isoformat()} 
                           for msg in edited_messages]
    conversation_state[refine_history_key] = {
        "status": "approved_edited",
        "messages": edited_messages_list,
        "timestamp": datetime.now().isoformat(),
        "user_id": user_id
    }
    
    # Clean up the conversation state
    del conversation_state[conversation_key]

# Handle /finalize command
@app.command("/finalize")
def handle_finalize_command(ack, say, command):
    ack()
    user_id = command["user_id"]
    channel_id = command["channel_id"]
    conversation_key = f"{user_id}_{channel_id}"
    
    # Check if there's a previous conversation
    if conversation_key not in conversation_state:
        say("No conversation found. Please use `/ask` first.")
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
    #     say(f"Error: {str(e)}")
    #     return
    
    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Response finalized!*"
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

# Handle /show-proposal command
@app.command("/show-proposal")
def handle_show_proposal_command(ack, say, command):
    ack()
    channel_id = command["channel_id"]
    
    # Show loading indicator
    say("Fetching proposal for this channel...\nPlease wait...")
    
    # Get project_id for this channel
    project_id = get_project_by_channel(channel_id)
    
    if not project_id:
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "*No Project Found*\n\nThis channel is not linked to any project. Please link a project to this channel first."
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
    #               "This is mock proposal data.",
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
                        "text": f"*Error*\n\n{proposal_data['error']}"
                    }
                }
            ]
        })
        return
    
    # Convert markdown to Slack format and split into chunks if needed
    content = proposal_data['content']
    slack_formatted_content = convert_markdown_to_slack(content)
    content_chunks = split_text_for_slack(slack_formatted_content, max_length=2800)
    
    # Build blocks dynamically with better structure
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "Project Proposal",
                "emoji": True
            }
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*Project:* `{proposal_data['project_id']}` • *Version:* {proposal_data['version']} • *Updated:* {proposal_data['timestamp']}"
                }
            ]
        },
        {
            "type": "divider"
        }
    ]
    
    # Add content blocks with better formatting
    for i, chunk in enumerate(content_chunks):
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": chunk
            }
        })
        
        # Add divider between chunks for better readability
        if i < len(content_chunks) - 1:
            blocks.append({"type": "divider"})
    
    # Add footer with actions
    blocks.extend([
        {
            "type": "divider"
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Use /refine-proposal to generate a refined version • /ask to ask questions about this proposal"
                }
            ]
        }
    ])
    
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
                    "text": "GammaBot Commands"
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
                    "text": "• `/ask <question>` - Ask the AI agent a question\n• `/show-proposal` - View the proposal for this channel\n• `/refine-proposal` - Refine the proposal with a different version\n• `/finalize` - Confirm and save the current response\n• `/status` - Check bot status\n• `/clear` - Clear your conversation history\n• `/help` - Show this help message"
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
                        "text": "*Example:* `/ask What is machine learning?`"
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
        agent_status = "Online" if response.status_code == 200 else "Limited"
    except:
        agent_status = "Offline"
    
    say({
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*GammaBot Status*\n\n• Bot: Online\n• Agent Server: {agent_status}\n• Active Sessions: {len(conversation_state)}"
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
        say("Your conversation history has been cleared.")
    else:
        say("No conversation history found.")

# Handle app mentions in channels
@app.event("app_mention")
def handle_app_mention(event, say):
    text = event.get("text", "").lower()
    user_id = event["user"]
    channel_id = event["channel"]
    
    # Remove bot mention from text
    text = text.split(maxsplit=1)
    if len(text) > 1:
        query = text[1].strip()
    else:
        say(f"Hi <@{user_id}>!\n\nUse `/help` to see available commands or mention me with a question!")
        return
    
    # Treat mentions as ask commands
    if query:
        say(f"Processing your question: _{query}_\nPlease wait...")
        
        # Get project_id and message history
        project_id = get_project_by_channel(channel_id)
        message_history = get_channel_message_history(channel_id, limit=50)
        
        # MOCK RESPONSE - Comment out for production
        result = {
            "response": f"This is a mock response to your question: '{query}'\n\n"
                       f"• Channel ID: {channel_id}\n"
                       f"• Project ID: {project_id or 'Not linked'}\n"
                       f"• Previous messages: {len(message_history)} found\n\n"
                       f"This is a test response. The actual agent API is not being called.",
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
        #     say(f"Error: {str(e)}")
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


    
    if text.lower() in ["help", "hello", "hi"]:
        say(f"Hi <@{user_id}>!\n\nI'm GammaBot, your AI assistant.\n\nUse `/help` to see all available commands or just type your question!")
    else:
        # Treat DM as ask command
        say(f"Processing your question...\nPlease wait...")
        
        # Get project_id and message history
        project_id = get_project_by_channel(channel_id)
        message_history = get_channel_message_history(channel_id, limit=50)
        
        # MOCK RESPONSE - Comment out for production
        result = {
            "response": f"This is a mock response to your question: '{text}'\n\n"
                       f"• Channel ID: {channel_id}\n"
                       f"• Project ID: {project_id or 'Not linked'}\n"
                       f"• Previous messages: {len(message_history)} found\n\n"
                       f"This is a test response. The actual agent API is not being called.",
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
        #     say(f"Error: {str(e)}")
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
                        "text": "Use /refine-proposal for another version or /finalize to confirm"
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
