import os
import json
import requests
import time
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

# Canvas storage file path
CANVAS_STORAGE_FILE = "canvas_storage.json"

# Load canvas storage from file
def load_canvas_storage():
    """Load canvas storage from JSON file"""
    if os.path.exists(CANVAS_STORAGE_FILE):
        try:
            with open(CANVAS_STORAGE_FILE, 'r') as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading canvas storage: {e}")
            return {}
    return {}

# Save canvas storage to file
def save_canvas_storage(storage):
    """Save canvas storage to JSON file"""
    try:
        with open(CANVAS_STORAGE_FILE, 'w') as f:
            json.dump(storage, f, indent=2)
    except Exception as e:
        print(f"Error saving canvas storage: {e}")

# Initialize canvas storage
canvas_storage = load_canvas_storage()

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

# Canvas Helper Functions
def get_canvas_ids(channel_id):
    """Get canvas IDs for a channel"""
    return canvas_storage.get(channel_id, {})

def store_canvas_ids(channel_id, requirements_canvas_id=None, proposal_canvas_id=None):
    """Store canvas IDs for a channel"""
    if channel_id not in canvas_storage:
        canvas_storage[channel_id] = {}
    
    if requirements_canvas_id:
        canvas_storage[channel_id]["requirements_canvas_id"] = requirements_canvas_id
    if proposal_canvas_id:
        canvas_storage[channel_id]["proposal_canvas_id"] = proposal_canvas_id
    
    save_canvas_storage(canvas_storage)

def create_canvas(channel_id, title, content):
    """
    Create a new canvas in a Slack channel
    Returns canvas_id or None if failed
    """
    try:
        # Convert markdown to canvas document format
        document_content = {
            "type": "markdown",
            "markdown": content
        }
        
        result = app.client.canvases_create(
            channel_id=channel_id,
            title=title,
            document_content=document_content
        )
        
        if result.get("ok"):
            canvas_id = result.get("canvas_id")
            print(f"Canvas created successfully: {canvas_id}")
            return canvas_id
        else:
            print(f"Failed to create canvas: {result}")
            return None
    except Exception as e:
        print(f"Error creating canvas: {e}")
        return None

def update_canvas(canvas_id, content):
    """
    Update an existing canvas with new content
    Returns True if successful, False otherwise
    """
    try:
        document_content = {
            "type": "markdown",
            "markdown": content
        }
        
        result = app.client.canvases_edit(
            canvas_id=canvas_id,
            changes=[{
                "operation": "replace",
                "document_content": document_content
            }]
        )
        
        if result.get("ok"):
            print(f"Canvas updated successfully: {canvas_id}")
            return True
        else:
            print(f"Failed to update canvas: {result}")
            return False
    except Exception as e:
        print(f"Error updating canvas: {e}")
        return False

def open_canvas(channel_id, canvas_id):
    """
    Post a message in the channel with a link to open the canvas
    """
    try:
        app.client.chat_postMessage(
            channel=channel_id,
            text=f"Click to view canvas",
            metadata={
                "event_type": "canvas_reference",
                "event_payload": {
                    "canvas_id": canvas_id
                }
            },
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📄 <slack://canvas/{canvas_id}|Open Canvas>"
                    }
                }
            ]
        )
        return True
    except Exception as e:
        print(f"Error posting canvas link: {e}")
        return False

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
    in_table = False
    table_headers = []
    table_aligns = []
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Handle code blocks
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            converted_lines.append('```')
            i += 1
            continue
        
        if in_code_block:
            converted_lines.append(line)
            i += 1
            continue
        
        # Detect markdown table start
        if not in_table and '|' in line and i + 1 < len(lines) and '|' in lines[i + 1]:
            next_line = lines[i + 1]
            # Check if next line is separator (contains dashes)
            if re.match(r'^\s*\|[\s\-:]+\|\s*$', next_line) or re.match(r'^\s*\|[\s\-:|]+', next_line):
                in_table = True
                # Parse headers
                table_headers = [cell.strip() for cell in line.split('|') if cell.strip()]
                # Parse alignment line
                align_cells = [cell.strip() for cell in next_line.split('|') if cell.strip()]
                table_aligns = []
                for cell in align_cells:
                    if cell.startswith(':') and cell.endswith(':'):
                        table_aligns.append('center')
                    elif cell.endswith(':'):
                        table_aligns.append('right')
                    else:
                        table_aligns.append('left')
                
                # Start table formatting
                converted_lines.append('\n```')
                # Create header row
                header_row = ' | '.join([f"{h:^20}" if j < len(table_aligns) and table_aligns[j] == 'center' 
                                         else f"{h:<20}" for j, h in enumerate(table_headers)])
                converted_lines.append(header_row)
                # Create separator
                converted_lines.append('-' * len(header_row))
                
                i += 2  # Skip header and separator lines
                continue
        
        # Process table rows
        if in_table and '|' in line:
            cells = [cell.strip() for cell in line.split('|') if cell.strip()]
            if cells:
                # Format row with proper spacing
                row = ' | '.join([f"{c:^20}" if j < len(table_aligns) and table_aligns[j] == 'center' 
                                  else f"{c:<20}" for j, c in enumerate(cells)])
                converted_lines.append(row)
                i += 1
                continue
        
        # End table if we encounter a non-table line
        if in_table and '|' not in line:
            in_table = False
            converted_lines.append('```\n')
            table_headers = []
            table_aligns = []
        
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
        
        i += 1
    
    # Close table if still open at end
    if in_table:
        converted_lines.append('```')
    
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

# Helper function to get project requirements
def get_project_requirements(project_id):
    """
    Get the original project requirements for a project
    Returns requirements data or error
    """
    try:
        response = requests.get(
            f"{AGENT_SERVER_URL}/project/{project_id}/requirements",
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        if response.status_code == 404:
            return {"error": "No requirements found for this project."}
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error fetching requirements for project {project_id}: {e}")
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
    
    # Convert markdown to Slack format and split into chunks if needed
    slack_formatted_response = convert_markdown_to_slack(response_text)
    response_chunks = split_text_for_slack(slack_formatted_response, max_length=2800)
    
    # Build blocks dynamically
    blocks = [
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
                "text": "*Response:*"
            }
        }
    ]
    
    # Add response chunks
    for i, chunk in enumerate(response_chunks):
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": chunk
            }
        })
        
        # Add divider between chunks for better readability
        if i < len(response_chunks) - 1:
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
                    "text": "Use /refine-proposal to get a different response or /finalize to confirm"
                }
            ]
        }
    ])
    
    say({"blocks": blocks})

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
    
    # Update canvas with refined proposal
    content = result.get("content", "No response received")
    
    # Get canvas IDs for this channel
    canvas_ids = get_canvas_ids(channel_id)
    proposal_canvas_id = canvas_ids.get("proposal_canvas_id")
    
    if proposal_canvas_id:
        # Update existing canvas
        updated = update_canvas(
            canvas_id=proposal_canvas_id,
            content=f"# Project Proposal\n\n**Project:** {result.get('project_id')} | **Version:** {result.get('version')}\n\n---\n\n{content}"
        )
        
        if updated:
            say({
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "✅ Proposal Canvas Updated"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📋 *Refined Proposal Generated*\n\n<slack://canvas/{proposal_canvas_id}|Open Updated Proposal Canvas>"
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
                    }
                ]
            })
        else:
            say("Failed to update proposal canvas. Please try again.")
    else:
        # Create new canvas if it doesn't exist
        proposal_canvas_id = create_canvas(
            channel_id=channel_id,
            title="📋 Project Proposal",
            content=f"# Project Proposal\n\n**Project:** {result.get('project_id')} | **Version:** {result.get('version')}\n\n---\n\n{content}"
        )
        
        if proposal_canvas_id:
            store_canvas_ids(channel_id, proposal_canvas_id=proposal_canvas_id)
            say({
                "blocks": [
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "✅ Proposal Canvas Created"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📋 *Refined Proposal Generated*\n\n<slack://canvas/{proposal_canvas_id}|Open Proposal Canvas>"
                        }
                    }
                ]
            })
        else:
            say("Failed to create proposal canvas. Please try again.")
    
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
    
    # Update canvas with refined proposal
    content = result.get("content", "No response received")
    
    # Get canvas IDs for this channel
    canvas_ids = get_canvas_ids(channel_id)
    proposal_canvas_id = canvas_ids.get("proposal_canvas_id")
    
    if proposal_canvas_id:
        # Update existing canvas
        updated = update_canvas(
            canvas_id=proposal_canvas_id,
            content=f"# Project Proposal\n\n**Project:** {result.get('project_id')} | **Version:** {result.get('version')}\n\n---\n\n{content}"
        )
        
        if updated:
            client.chat_postMessage(
                channel=channel_id,
                blocks=[
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "✅ Proposal Canvas Updated (Edited)"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📋 *Refined Proposal Generated*\n\n<slack://canvas/{proposal_canvas_id}|Open Updated Proposal Canvas>"
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
                    }
                ]
            )
        else:
            client.chat_postMessage(
                channel=channel_id,
                text="Failed to update proposal canvas. Please try again."
            )
    else:
        # Create new canvas if it doesn't exist
        proposal_canvas_id = create_canvas(
            channel_id=channel_id,
            title="📋 Project Proposal",
            content=f"# Project Proposal\n\n**Project:** {result.get('project_id')} | **Version:** {result.get('version')}\n\n---\n\n{content}"
        )
        
        if proposal_canvas_id:
            store_canvas_ids(channel_id, proposal_canvas_id=proposal_canvas_id)
            client.chat_postMessage(
                channel=channel_id,
                blocks=[
                    {
                        "type": "header",
                        "text": {
                            "type": "plain_text",
                            "text": "✅ Proposal Canvas Created"
                        }
                    },
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": f"📋 *Refined Proposal Generated*\n\n<slack://canvas/{proposal_canvas_id}|Open Proposal Canvas>"
                        }
                    }
                ]
            )
        else:
            client.chat_postMessage(
                channel=channel_id,
                text="Failed to create proposal canvas. Please try again."
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
    
    # Get canvas IDs for this channel
    canvas_ids = get_canvas_ids(channel_id)
    proposal_canvas_id = canvas_ids.get("proposal_canvas_id")
    
    # If canvas exists, post link to it
    if proposal_canvas_id:
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📋 *Project Proposal*\n\n<slack://canvas/{proposal_canvas_id}|Open Proposal Canvas>"
                    }
                }
            ]
        })
        return
    
    # If no canvas exists, fetch and create one
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
    
    # Get proposal data
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
    
    # Create canvas with proposal
    proposal_content = proposal_data.get('content', '')
    proposal_canvas_id = create_canvas(
        channel_id=channel_id,
        title="📋 Project Proposal",
        content=f"# Project Proposal\n\n**Project:** {proposal_data.get('project_id')} | **Version:** {proposal_data.get('version')}\n\n---\n\n{proposal_content}"
    )
    
    if proposal_canvas_id:
        # Store canvas ID
        store_canvas_ids(channel_id, proposal_canvas_id=proposal_canvas_id)
        
        # Post link to canvas
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📋 *Project Proposal*\n\n<slack://canvas/{proposal_canvas_id}|Open Proposal Canvas>"
                    }
                }
            ]
        })
    else:
        say("Failed to create proposal canvas. Please try again.")

# Handle /show-requirements command
@app.command("/show-requirements")
def handle_show_requirements_command(ack, say, command):
    ack()
    channel_id = command["channel_id"]
    
    # Get canvas IDs for this channel
    canvas_ids = get_canvas_ids(channel_id)
    requirements_canvas_id = canvas_ids.get("requirements_canvas_id")
    
    # If canvas exists, post link to it
    if requirements_canvas_id:
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📝 *Project Requirements*\n\n<slack://canvas/{requirements_canvas_id}|Open Requirements Canvas>"
                    }
                }
            ]
        })
        return
    
    # If no canvas exists, fetch and create one
    say("Fetching project requirements for this channel...\nPlease wait...")
    
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
    
    # Get project requirements
    requirements_data = get_project_requirements(project_id)
    
    if "error" in requirements_data:
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Error*\n\n{requirements_data['error']}"
                    }
                }
            ]
        })
        return
    
    # Create canvas with requirements
    requirements_text = requirements_data.get('requirements', '')
    requirements_canvas_id = create_canvas(
        channel_id=channel_id,
        title="📝 Project Requirements",
        content=f"# Project Requirements\n\n**Project:** {requirements_data.get('project_id')}\n\n---\n\n{requirements_text}"
    )
    
    if requirements_canvas_id:
        # Store canvas ID
        store_canvas_ids(channel_id, requirements_canvas_id=requirements_canvas_id)
        
        # Post link to canvas
        say({
            "blocks": [
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📝 *Project Requirements*\n\n<slack://canvas/{requirements_canvas_id}|Open Requirements Canvas>"
                    }
                }
            ]
        })
    else:
        say("Failed to create requirements canvas. Please try again.")

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
                    "text": "• `/ask <question>` - Ask the AI agent a question\n• `/show-requirements` - View the original project requirements\n• `/show-proposal` - View the proposal for this channel\n• `/refine-proposal` - Refine the proposal with a different version\n• `/finalize` - Confirm and save the current response\n• `/status` - Check bot status\n• `/clear` - Clear your conversation history\n• `/help` - Show this help message"
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
        
        # Convert markdown to Slack format and split into chunks if needed
        slack_formatted_response = convert_markdown_to_slack(response_text)
        response_chunks = split_text_for_slack(slack_formatted_response, max_length=2800)
        
        # Build blocks dynamically
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Response:*"
                }
            }
        ]
        
        # Add response chunks
        for i, chunk in enumerate(response_chunks):
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": chunk
                }
            })
            
            # Add divider between chunks for better readability
            if i < len(response_chunks) - 1:
                blocks.append({"type": "divider"})
        
        say({"blocks": blocks})

# Handle when bot is added to a channel
@app.event("member_joined_channel")
def handle_member_joined_channel(event, say):
    """
    Triggered when bot is added to a channel.
    Creates canvases for requirements and proposal if project is linked.
    """
    user = event.get("user")
    channel = event.get("channel")
    
    # Check if the bot itself was added (not another user)
    try:
        bot_user_id = app.client.auth_test()["user_id"]
    except Exception as e:
        print(f"Error getting bot user ID: {e}")
        return
    
    if user == bot_user_id:
        print(f"Bot added to channel: {channel}")
        
        # Send a welcome message
        say("👋 Hello! I've been added to this channel. Let me set up project canvases for you...")
        
        # Wait 5 seconds before fetching and creating canvases
        time.sleep(5)
        
        # Get project_id for this channel
        project_id = get_project_by_channel(channel)
        
        if not project_id:
            say({
                "blocks": [
                    {
                        "type": "section",
                        "text": {
                            "type": "mrkdwn",
                            "text": "⚠️ *No Project Linked*\n\nThis channel is not linked to any project yet. Please link it first using your dashboard."
                        }
                    },
                    {
                        "type": "context",
                        "elements": [
                            {
                                "type": "mrkdwn",
                                "text": "Use /help to see available commands"
                            }
                        ]
                    }
                ]
            })
            return
        
        # Get the project requirements
        requirements_data = get_project_requirements(project_id)
        
        # Get the latest proposal
        proposal_data = get_latest_proposal(project_id)
        
        # Check for errors
        requirements_error = "error" in requirements_data
        proposal_error = "error" in proposal_data
        
        if requirements_error and proposal_error:
            say(f"❌ Could not fetch project details:\n• Requirements: {requirements_data.get('error', 'Unknown error')}\n• Proposal: {proposal_data.get('error', 'Unknown error')}")
            return
        
        # Create canvases
        requirements_canvas_id = None
        proposal_canvas_id = None
        
        # === CREATE REQUIREMENTS CANVAS ===
        if not requirements_error:
            requirements_text = requirements_data.get('requirements', '')
            requirements_canvas_id = create_canvas(
                channel_id=channel,
                title="📝 Project Requirements",
                content=f"# Project Requirements\n\n**Project:** {requirements_data.get('project_id')}\n\n---\n\n{requirements_text}"
            )
            
            if requirements_canvas_id:
                print(f"Requirements canvas created: {requirements_canvas_id}")
            else:
                print("Failed to create requirements canvas")
        
        # === CREATE PROPOSAL CANVAS ===
        if not proposal_error:
            proposal_content = proposal_data.get('content', '')
            proposal_canvas_id = create_canvas(
                channel_id=channel,
                title="📋 Project Proposal",
                content=f"# Project Proposal\n\n**Project:** {proposal_data.get('project_id')} | **Version:** {proposal_data.get('version')}\n\n---\n\n{proposal_content}"
            )
            
            if proposal_canvas_id:
                print(f"Proposal canvas created: {proposal_canvas_id}")
            else:
                print("Failed to create proposal canvas")
        
        # Store canvas IDs
        if requirements_canvas_id or proposal_canvas_id:
            store_canvas_ids(channel, requirements_canvas_id, proposal_canvas_id)
        
        # Post welcome message with canvas links
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "✅ Project Workspace Ready",
                    "emoji": True
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Your project canvases have been created for *{project_id}*"
                }
            },
            {
                "type": "divider"
            }
        ]
        
        if requirements_canvas_id:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📝 *Requirements Canvas*\n<slack://canvas/{requirements_canvas_id}|Open Requirements>"
                }
            })
        
        if proposal_canvas_id:
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📋 *Proposal Canvas*\n<slack://canvas/{proposal_canvas_id}|Open Proposal>"
                }
            })
        
        blocks.extend([
            {
                "type": "divider"
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "💬 Use `/show-requirements` or `/show-proposal` to view canvases • `/refine-proposal` to update • `/help` for all commands"
                    }
                ]
            }
        ])
        
        say({"blocks": blocks})
        
        say({"blocks": blocks})

# Handle channel creation events (optional - for logging/tracking)
@app.event("channel_created")
def handle_channel_created(event):
    """
    Triggered when a new channel is created.
    Used for logging and tracking purposes.
    """
    channel_info = event.get("channel", {})
    channel_id = channel_info.get("id")
    channel_name = channel_info.get("name")
    creator = event.get("channel", {}).get("creator")
    
    print(f"📢 New channel created: {channel_name} ({channel_id}) by user {creator}")
    
    # You can add additional logic here if needed
    # For example, check if the channel name matches a pattern
    # or notify an admin channel about new channels

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
        
        # Convert markdown to Slack format and split into chunks if needed
        slack_formatted_response = convert_markdown_to_slack(response_text)
        response_chunks = split_text_for_slack(slack_formatted_response, max_length=2800)
        
        # Build blocks dynamically
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Response:*"
                }
            }
        ]
        
        # Add response chunks
        for i, chunk in enumerate(response_chunks):
            blocks.append({
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": chunk
                }
            })
            
            # Add divider between chunks for better readability
            if i < len(response_chunks) - 1:
                blocks.append({"type": "divider"})
        
        # Add footer
        blocks.append({
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": "Use /refine-proposal for another version or /finalize to confirm"
                }
            ]
        })
        
        say({"blocks": blocks})

@flask_app.route("/slack", methods=["POST"])
def slack_handler():
    return handler.handle(request)

@flask_app.route("/health", methods=["GET"])
def health_check():
    return {"status": "healthy", "bot": "GammaBot"}, 200

@flask_app.route("/channel-created", methods=["POST"])
def handle_channel_created_webhook():
    """
    Webhook endpoint for frontend to notify bot after channel creation.
    Creates canvases for requirements and proposal in the new channel.
    
    Expected payload:
    {
        "channel_id": "C1234567890",
        "project_id": "proj_123"
    }
    """
    try:
        data = request.json
        channel_id = data.get("channel_id")
        project_id = data.get("project_id")
        
        if not channel_id or not project_id:
            return {"error": "Missing channel_id or project_id"}, 400
        
        print(f"Frontend notification: Channel {channel_id} created for project {project_id}")
        
        # Wait 5 seconds before fetching and creating canvases
        time.sleep(5)
        
        # Get the project requirements
        requirements_data = get_project_requirements(project_id)
        
        # Get the proposal
        proposal_data = get_latest_proposal(project_id)
        
        # Check for errors
        requirements_error = "error" in requirements_data
        proposal_error = "error" in proposal_data
        
        if requirements_error and proposal_error:
            return {
                "error": "Could not fetch project details",
                "requirements_error": requirements_data.get('error'),
                "proposal_error": proposal_data.get('error')
            }, 404
        
        # Create canvases instead of posting messages
        requirements_canvas_id = None
        proposal_canvas_id = None
        
        # === CREATE REQUIREMENTS CANVAS ===
        if not requirements_error:
            requirements_text = requirements_data.get('requirements', '')
            requirements_canvas_id = create_canvas(
                channel_id=channel_id,
                title="📝 Project Requirements",
                content=f"# Project Requirements\n\n**Project:** {requirements_data.get('project_id')}\n\n---\n\n{requirements_text}"
            )
            
            if requirements_canvas_id:
                print(f"Requirements canvas created: {requirements_canvas_id}")
            else:
                print("Failed to create requirements canvas")
        
        # === CREATE PROPOSAL CANVAS ===
        if not proposal_error:
            proposal_content = proposal_data.get('content', '')
            proposal_canvas_id = create_canvas(
                channel_id=channel_id,
                title="📋 Project Proposal",
                content=f"# Project Proposal\n\n**Project:** {proposal_data.get('project_id')} | **Version:** {proposal_data.get('version')}\n\n---\n\n{proposal_content}"
            )
            
            if proposal_canvas_id:
                print(f"Proposal canvas created: {proposal_canvas_id}")
            else:
                print("Failed to create proposal canvas")
        
        # Store canvas IDs
        if requirements_canvas_id or proposal_canvas_id:
            store_canvas_ids(channel_id, requirements_canvas_id, proposal_canvas_id)
        
        # Post welcome message with canvas links
        try:
            blocks = [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "🎉 Project Workspace Created",
                        "emoji": True
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"Welcome! Your project canvases have been created for *{project_id}*"
                    }
                },
                {
                    "type": "divider"
                }
            ]
            
            if requirements_canvas_id:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📝 *Requirements Canvas*\n<slack://canvas/{requirements_canvas_id}|Open Requirements>"
                    }
                })
            
            if proposal_canvas_id:
                blocks.append({
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"📋 *Proposal Canvas*\n<slack://canvas/{proposal_canvas_id}|Open Proposal>"
                    }
                })
            
            blocks.extend([
                {
                    "type": "divider"
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "💬 Use `/show-requirements` to view requirements • `/show-proposal` to view proposal • `/refine-proposal` to update • `/help` for all commands"
                        }
                    ]
                }
            ])
            
            app.client.chat_postMessage(
                channel=channel_id,
                text="Project Requirements and Proposal",
                blocks=blocks
            )
        except Exception as e:
            print(f"Error posting welcome message: {e}")
        
        return {
            "status": "success",
            "message": "Canvases created successfully",
            "requirements_canvas_id": requirements_canvas_id,
            "proposal_canvas_id": proposal_canvas_id
        }, 200
    
    except Exception as e:
        print(f"Error in channel_created webhook: {e}")
        return {"error": str(e)}, 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    flask_app.run(debug=False, host="0.0.0.0", port=port)
