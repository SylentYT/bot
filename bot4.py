import os
import logging
from datetime import datetime, timedelta
import time
import threading
import mysql.connector
from exceptiongroup import catch
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaDocument, ForceReply
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from dotenv import load_dotenv

load_dotenv()
database = None

# Configure logging
logging.basicConfig(filename='logs/bot_activity.log', level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Telegram bot token (replace 'YOUR_TOKEN' with your actual bot token)
TOKEN = os.environ.get('TOKEN')
# Group ID that serves as the whitelist
WHITELIST_GROUP_ID = os.environ.get('WHITELIST_GROUP_ID')  # Replace with your whitelist group ID
# Group ID where the images will be reposted
TARGET_GROUP_ID = os.environ.get('TARGET_GROUP_ID')  # Replace with your target group ID

DEFAULT_STATE, IMAGE_PROCESSING, JOIN_PROCESSING, SUBMITTING_TICKET, ANNOUNCEMENT, BAN_ZONE = range(6)

# Cache structure: {user_id: {'count': X, 'last_access': datetime}}
start_command_usage = {}

# Buttons and Rows
cancelbtn = [[InlineKeyboardButton("Cancel", callback_data='cancel')]]
submit_button = [[InlineKeyboardButton("Submit", callback_data='submit')]]
join_button = [[InlineKeyboardButton("Join", callback_data='joinbtn')]]

row1 = [[InlineKeyboardButton("Open Ticket", callback_data='ticket'),
         InlineKeyboardButton("Send Images", callback_data='imagebutton')],
        [InlineKeyboardButton("Groups", callback_data='groups_join')]]

# Functions
async def log_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    logging.info(f"User {user.id} ({user.username}): {update.message.text}")


async def check_user_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if update.message.chat.type == "private":
        user_id = update.effective_user.id
        db_connection = connect_to_database()
        if not db_connection:
            print("Failed to connect to the database")
            return "error"

        cursor = db_connection.cursor()
        cursor.execute("SELECT status, cooldown_until FROM users WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()

        now = datetime.now()

        if result:
            user_status, cooldown_until = result
            if user_status == 'ban':
                return 'ban'
            elif user_status == 'pending':
                if cooldown_until and now < cooldown_until:
                    remaining_seconds = int((cooldown_until - now).total_seconds())
                    return f"cooldown_{remaining_seconds}"
                else:
                    member_status = await get_whitelist_membership_status(context, user_id)
                    if member_status == "member":
                        cursor.execute("UPDATE users SET status = %s WHERE user_id = %s", ('member', user_id))
                        db_connection.commit()
                        return "member"
                    else:
                        cooldown_until = now + timedelta(hours=6)
                        cursor.execute("UPDATE users SET cooldown_until = %s WHERE user_id = %s", (cooldown_until, user_id))
                        db_connection.commit()
                        return "pending_not_member"
            elif user_status == 'member':
                member_status = await get_whitelist_membership_status(context, user_id)
                if member_status != "member":
                    cursor.execute("UPDATE users SET status = %s WHERE user_id = %s", ('pending', user_id))
                    db_connection.commit()
                    return "removed_member"
                return 'member'
        else:
            # User not found in the database, check if they are a member of the whitelist group
            member_status = await get_whitelist_membership_status(context, user_id)
            if member_status == "member":
                try:
                    cursor.execute("INSERT INTO users (user_id, status) VALUES (%s, 'member')", (user_id,))
                    db_connection.commit()
                    print(f"Added user {user_id} as 'member'.")
                    return "member"
                except mysql.connector.Error as e:
                    print(f"Failed to insert user {user_id} into database: {e}")
                    return "error"
            else:
                # User is not a member of the whitelist group, return as "new_user"
                return "new_user"

        cursor.close()
        db_connection.close()
        return "error"


async def get_whitelist_membership_status(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> str:
    try:
        member = await context.bot.get_chat_member(WHITELIST_GROUP_ID, user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return "member"
    except Exception as e:
        print(f"Error checking whitelist membership: {e}")
    return "not_member"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.chat.type == "private":
        user_id = update.effective_user.id
        username = update.effective_user.username or "No username"
        now = datetime.now()
        topic_message_id = 149  # Replace with the actual ID of the topic message
        current_status = await check_user_membership(update, context)

        if current_status.startswith("pending") or current_status.startswith("cooldown_"):
        # Apply rate-limiting logic if user status starts with "pending"
            user_data = start_command_usage.get(user_id, {'count': 0, 'last_usage': now - timedelta(hours=1)})
            if now - user_data['last_usage'] < timedelta(hours=1):
                user_data['count'] += 1
                if user_data['count'] > 5:
                    # Update the user's status to "ban" in the database
                    await ban_user(user_id)
                    notification_message = f"User @{username} (ID: {user_id}) has been banned for excessive usage."
                    await context.bot.send_message(chat_id=TARGET_GROUP_ID, text=notification_message, reply_to_message_id=topic_message_id)
                    return ConversationHandler.END
            else:
                user_data['count'] = 1  # Reset count if last usage was more than an hour ago
            
            user_data['last_usage'] = now
            start_command_usage[user_id] = user_data

        status = await check_user_membership(update, context)
        if status in ["new_user", "removed_member"]:
            # User not in database; prompt to join the whitelist group
            reply_markup = InlineKeyboardMarkup(join_button)
            await update.message.reply_text("You need to join the whitelist group first.", reply_markup=reply_markup)
            return JOIN_PROCESSING
        elif status == "ban":
            # Banned users get a message; you might implement a more restrictive approach as needed
            await update.message.reply_text("You are banned from using this bot.")
            return ConversationHandler.END
        elif status.startswith("cooldown"):
            remaining_seconds = int(status.split('_')[1])
            hours, remainder_seconds = divmod(remaining_seconds, 3600)
            minutes, seconds = divmod(remainder_seconds, 60)
            cooldown_message = f"You are currently on cooldown. Please wait {hours} hours, {minutes} minutes, and {seconds} seconds before trying again." if hours else f"You are currently on cooldown. Please wait {minutes} minutes and {seconds} seconds before trying again."
            await update.message.reply_text(cooldown_message)
            return ConversationHandler.END
        elif status == "pending_not_member":
            await update.message.reply_text("Please check your DMs or contact a moderator directly.")
            return ConversationHandler.END
        elif status in ["member", "pending_member"]:
            # Direct members to the main functionality of the bot
            reply_markup = InlineKeyboardMarkup(row1)
            await update.message.reply_text("Hi! Please choose an option:", reply_markup=reply_markup)
            return DEFAULT_STATE
        else:
            # Handle any other cases or errors
            await update.message.reply_text("An error occurred. Please try again later /start.")
            return ConversationHandler.END

async def ban_user(user_id: int):
        db_connection = connect_to_database()
        if db_connection:
            cursor = db_connection.cursor()
            cursor.execute("UPDATE users SET status = 'ban' WHERE user_id = %s", (user_id,))
            db_connection.commit()
            cursor.close()
            db_connection.close()

async def unban_user(user_id: str, context: ContextTypes.DEFAULT_TYPE):
    db_connection = connect_to_database()
    if db_connection:
        cursor = db_connection.cursor()
        cursor.execute("UPDATE users SET status = 'pending' WHERE user_id = %s", (user_id,))
        db_connection.commit()
        cursor.close()
        db_connection.close()

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        # Assuming there's a predefined list or a specific chat where admin status is checked
        admin_chat_id = WHITELIST_GROUP_ID  # The chat ID of your admin group
        
        user_id = update.effective_user.id
        target_user_id = context.args[0] if context.args else None

        if not target_user_id:
            await update.message.reply_text("Usage: /unban <user_id>")
            return

        try:
            # Check if the issuing user is an admin or creator in the admin group
            admin_status = await context.bot.get_chat_member(admin_chat_id, user_id)
            if admin_status.status not in ['administrator', 'creator']:
                await update.message.reply_text("You do not have permission to use this command.")
                return

            # Proceed to unban the target user by setting their status to 'pending'
            await unban_user(target_user_id, context)
            await update.message.reply_text(f"User {target_user_id} has been unbanned and set to pending status.")
        except Exception as e:
            await update.message.reply_text("An error occurred while processing the unban command.")
            print(f"Error in unban_command: {e}")

async def process_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.chat.type == "private": 
        return IMAGE_PROCESSING  # Only process from private chats

    if update.message.photo:
        media, caption = await handle_photo(update)
    elif update.message.document:
        media, caption = await handle_document(update)
    else:
        reply_markup = InlineKeyboardMarkup(cancelbtn)
        await update.message.reply_text('Please send an image or a supported document type.', reply_markup=reply_markup)
        return IMAGE_PROCESSING

    if media:
        await send_media(context, media, caption)
        reply_markup = InlineKeyboardMarkup(cancelbtn)
        await update.message.reply_text('Image/document processed. Send another image/document.', reply_markup=reply_markup)
    else:
        reply_markup = InlineKeyboardMarkup(cancelbtn)
        await update.message.reply_text('Failed to process the image/document. Try again.', reply_markup=reply_markup)

    return IMAGE_PROCESSING  

async def handle_photo(update: Update) -> (InputMediaPhoto):
    photo = update.message.photo[-1] 
    caption = generate_caption(update)
    return InputMediaPhoto(photo.file_id, caption=caption), caption

async def handle_document(update: Update) -> (InputMediaDocument):
    # Add more specific checks for supported document types (if necessary)
    document = update.message.document
    caption = generate_caption(update) 
    return InputMediaDocument(document.file_id, caption=caption), caption

async def send_media(context: ContextTypes.DEFAULT_TYPE, media, caption):
    print(f"Media type: {type(media)}")  # Check the type of media being passed 
    print(f"Target Group ID: {TARGET_GROUP_ID}") 
    print(f"Topic Message ID: {topic_message_id}")
    # Assuming 'topic_message_id' is the message ID you want to reply to in the target group.
    topic_message_id = 22  # Replace with the actual ID of the topic message
    if isinstance(media, InputMediaPhoto):
        await context.bot.send_photo(chat_id=TARGET_GROUP_ID, photo=media.media, caption=caption, reply_to_message_id=topic_message_id)
    elif isinstance(media, InputMediaDocument):
        await context.bot.send_document(chat_id=TARGET_GROUP_ID, document=media.media, caption=caption, reply_to_message_id=topic_message_id)


async def generate_caption(update: Update) -> str:
    user = update.effective_user
    original_caption = update.message.caption if update.message.caption else "No caption"
    return f"From: {user.first_name or 'No name'} @{user.username or 'No username'}\nCaption: {original_caption}"

async def handle_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.chat.type == "private":
        await log_message(update, context)  # Ensure log_message is adapted for async execution
        
        reply_markup = InlineKeyboardMarkup(row1)
        await update.message.reply_text('Please send an image or a file, and put the credits in the caption123.', reply_markup=reply_markup)

async def handle_non_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.chat.type == "private":
        await log_message(update, context)  # Ensure log_message is adapted for async execution
        await update.message.reply_text('Please test')

def build_group_selection_markup(groups, selected_groups):
    group_buttons = [InlineKeyboardButton(f"{'✓' if str(group[0]) in selected_groups else ''} {group[1]}", callback_data=f'group_{group[0]}') for group in groups]
    action_buttons_row = [InlineKeyboardButton("Submit", callback_data='submit'), InlineKeyboardButton("Cancel", callback_data='cancel')]

    keyboard = [group_buttons[i:i + 2] for i in range(0, len(group_buttons), 2)]

        # Adding the action buttons row to the keyboard layout
    keyboard.append(action_buttons_row)

    return InlineKeyboardMarkup(keyboard)

async def announcement_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type == "private":
        user_id = update.effective_user.id
        member = await context.bot.get_chat_member(WHITELIST_GROUP_ID, user_id)
        if member.status in ['administrator', 'creator']:
            await update.message.reply_text("Please send the announcement text.")
            return ANNOUNCEMENT  # Transition to the ANNOUNCEMENT state awaiting the text
        else:
            await update.message.reply_text("You don't have permission to use this command.")
            return JOIN_PROCESSING
    
async def capture_announcement_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Store the announcement temporarily in context for this user
    context.user_data['announcement'] = update.message.text
    
    # Show how the announcement will appear and ask for confirmation
    confirmation_keyboard = [[InlineKeyboardButton("Send", callback_data='send_announcement'), InlineKeyboardButton("Cancel", callback_data='cancel')]]
    reply_markup = InlineKeyboardMarkup(confirmation_keyboard)
    await update.message.reply_text (f"Preview:\n{update.message.text}", reply_markup=reply_markup)
    return DEFAULT_STATE  # Assuming you handle the confirmation in another state or reset/end the conversation

async def submit_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    topic_message_id = 146
    category_id = context.user_data['selected_category_id']  # Set this when the user selects a category
    message = update.message.text  # The content of the ticket

    db_connection = connect_to_database()
    if not db_connection:
        await update.message.reply_text("Failed to connect to the database. Please try again later.")
        return
    
    cursor = db_connection.cursor()
    cursor.execute(
        "INSERT INTO ticket (category_id, user_id, message) VALUES (%s, %s, %s)",
        (category_id, user_id, message)
    )
    db_connection.commit()
    cursor.close()
    db_connection.close()

    # Forward the ticket to the target group
    ticket_message = f"New ticket from @{update.effective_user.username} in category {category_id}: {message}"
    await context.bot.send_message(chat_id=TARGET_GROUP_ID, text=ticket_message, reply_to_message_id=topic_message_id)

    await update.message.reply_text("Your ticket has been submitted.")

async def category_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category_id = query.data.split('_')[1]
    context.user_data['selected_category_id'] = category_id  # Store selected category ID
    
    # Prompt user to enter ticket message
    await query.edit_message_text(text="Please enter the details of your ticket:")
    return SUBMITTING_TICKET

async def button_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    print(f"Received callback data: {query.data}")  # Add logging or print statement

    if query.data == 'cancel':
        context.user_data.clear()  # Optionally clear user data
        await query.edit_message_text(text="Operation cancelled.")
        return DEFAULT_STATE
    
    elif query.data == 'imagebutton':
        chat_id = query.message.chat_id
        reply_markup = InlineKeyboardMarkup(cancelbtn)
        await context.bot.send_message(chat_id=chat_id, text="Please send an image or a file, and put the credits in the caption.", reply_markup=reply_markup)
        return IMAGE_PROCESSING
    
    elif query.data == 'groups_join':

        db_connection = connect_to_database()
        if not db_connection:
            await query.edit_message_text(text="Failed to connect to the database. Please try again later.")
            return JOIN_PROCESSING

        cursor = db_connection.cursor()
        cursor.execute("SELECT group_id, group_name FROM groups_list")
        groups = cursor.fetchall()
        cursor.close()
        db_connection.close()

        selected_groups = context.user_data.get('selected_groups', [])
        reply_markup = build_group_selection_markup(groups, selected_groups)
        await query.edit_message_text(text="Select the groups you want to join:", reply_markup=reply_markup)
        return JOIN_PROCESSING
    
    elif query.data.startswith('group_'):
        group_id = query.data.split('_')[1]
        selected_groups = context.user_data.get('selected_groups', [])
        
        if group_id in selected_groups:
            selected_groups.remove(group_id)
        else:
            selected_groups.append(group_id)

        context.user_data['selected_groups'] = selected_groups

        # Fetch groups again from the database for the updated markup
        db_connection = connect_to_database()
        cursor = db_connection.cursor()
        cursor.execute("SELECT group_id, group_name FROM groups_list")
        groups = cursor.fetchall()
        cursor.close()
        db_connection.close()

        groups_list_text = "You selected:\n" + "\n".join([group[1] for group in groups if str(group[0]) in selected_groups]) if selected_groups else "No groups selected."
        reply_markup = build_group_selection_markup(groups, selected_groups)
        await query.edit_message_text(text=groups_list_text, reply_markup=reply_markup)
        return JOIN_PROCESSING
    
    elif query.data == 'joinbtn':
        user_id = query.from_user.id
        topic_message_id = 121

        db_connection = connect_to_database()

        if not db_connection:
            print("Failed to connect to the database")
            await query.edit_message_text(text="An error occurred. Please try again later.")
            return ConversationHandler.END

        cursor = db_connection.cursor()
        cursor.execute("SELECT cooldown_until FROM users WHERE user_id = %s", (user_id,))
        result = cursor.fetchone()
        
        now = datetime.now()
        if result and result[0] and now < result[0]:
            cooldown_until = result[0]
            remaining_cooldown = cooldown_until - now
            total_seconds = int(remaining_cooldown.total_seconds())
            hours, remainder_seconds = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder_seconds, 60)  # Use the remainder for minutes/seconds calculation
            
            if hours > 0:
                cooldown_message = f"You are currently on cooldown. Please wait {int(hours)} hours, {int(minutes)} minutes and {int(seconds)} seconds before trying again."
            else:
                cooldown_message = f"You are currently on cooldown. Please wait {int(minutes)} minutes and {int(seconds)} seconds before trying again."
            await query.edit_message_text(text=cooldown_message)
        else:
            cooldown_until = now + timedelta(hours=6)  # Adjust the cooldown duration as needed
            cursor.execute("INSERT INTO users (user_id, status, cooldown_until) VALUES (%s, 'pending', %s) ON DUPLICATE KEY UPDATE status = 'pending', cooldown_until = %s", 
                        (user_id, cooldown_until, cooldown_until))
            db_connection.commit()

            if not query.from_user.username:
                full_name = query.from_user.first_name + (" " + query.from_user.last_name if query.from_user.last_name else "")
                mod_message = f"User ID: {user_id}, Name: {full_name}. The user does not have a username. Please review their request."
            else:
                mod_message = f"User ID: {user_id}, Username: @{query.from_user.username}, The user does not have a username. Please review their request"

            await context.bot.send_message(chat_id=TARGET_GROUP_ID, text=mod_message, reply_to_message_id=topic_message_id)
            await query.edit_message_text(text="Your request to join has been registered. Please wait for approval.")

        cursor.close()
        db_connection.close()
        return ConversationHandler.END


    elif query.data == 'submit':
        topic_message_id = 124  # Ensure correct ID
        selected_groups = context.user_data.get('selected_groups', [])
        
        if not selected_groups:
            await query.edit_message_text(text="Please select at least one group before submitting.")
            return JOIN_PROCESSING

        message_text = f"Username: @{query.from_user.username} selected the following groups: {' ,'.join(selected_groups)}"
        await context.bot.send_message(chat_id=TARGET_GROUP_ID, text=message_text, reply_to_message_id=topic_message_id)
        
        await query.edit_message_text(text="Your selections have been submitted.")
        context.user_data.clear()
        return DEFAULT_STATE
    
    elif query.data == 'send_announcement':
        announcement = context.user_data.get('announcement')
        if announcement:
            sent_message = await context.bot.send_message(chat_id=WHITELIST_GROUP_ID, text=announcement)
            # Pin the sent announcement message in the group
            await context.bot.pin_chat_message(chat_id=WHITELIST_GROUP_ID, message_id=sent_message.message_id, disable_notification=False)
            await query.edit_message_text("Your announcement has been sent.")
            context.user_data.clear()
            # Ensure the conversation state is reset or ended appropriately
            return DEFAULT_STATE  # Or return to DEFAULT_STATE if you have specific handling for it
        
    elif query.data == 'ticket':
        db_connection = connect_to_database()
        if not db_connection:
            await query.edit_message_text(text="Failed to connect to the database. Please try again later.")
            return DEFAULT_STATE  # Make sure this return is inside the if block
        
        cursor = db_connection.cursor()
        cursor.execute("SELECT id, category_name, description FROM ticket_category")
        categories = cursor.fetchall()
        cursor.close()
        db_connection.close()

        # Preparing a message with categories and their descriptions
        categories_text = "Select a ticket category based on the descriptions below:\n\n"
        for id, name, description in categories:
            categories_text += f"*{name}*:\n{description}\n\n"
        
        # Edit the current message to show category names and descriptions
        await query.edit_message_text(text=categories_text, parse_mode='Markdown')

        # Prepare the keyboard for category selection
        keyboard_buttons = [InlineKeyboardButton(category[1], callback_data=f"category_{category[0]}") for category in categories]
        # Organizing two buttons per row
        keyboard_rows = [keyboard_buttons[i:i+2] for i in range(0, len(keyboard_buttons), 2)]
        # Adding the Cancel button in the last row
        keyboard_rows.append([InlineKeyboardButton("Cancel", callback_data='cancel')])

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

    # Sending a new message for category selection
    await query.message.reply_text("Please choose a category:", reply_markup=reply_markup)

async def error(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Update {update} caused error {context.error}")

def connect_to_database():
    try:
        # Replace the placeholder values with your database connection details
        connection = mysql.connector.connect(
            host=os.environ.get('DB_CONNECTION'),
            database=os.environ.get('DB_NAME'),
            user=os.environ.get('DB_USER'),
            password=os.environ.get('DB_PASSWORD')
        )
        if connection.is_connected():
            return connection
    except mysql.connector.Error as e:
        print(f"Error connecting to MySQL: {e}")

def execute_query(connection, query):
    cursor = connection.cursor()
    cursor.execute(query)
    # For retrieval queries
    records = cursor.fetchall()
    for row in records:
        print(row)
    # Remember to close the cursor and connection if you're done using them
    cursor.close()

def db_initialize():
    # SQL statements to create tables
    sql_statements = [
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT UNSIGNED NOT NULL PRIMARY KEY,
            member_status VARCHAR(50) NOT NULL DEFAULT 'pending',
            status VARCHAR(50) NOT NULL DEFAULT 'pending',  -- 'member', 'pending', or 'ban'
            cooldown_until DATETIME NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS groups_list (
            group_id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            group_name VARCHAR(255) NOT NULL,
            UNIQUE KEY unique_group_name (group_name)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS ticket_category (
            id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            category_name VARCHAR(255) NOT NULL,
            UNIQUE KEY unique_category_name (category_name)
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS ticket (
            id INT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
            category_id INT UNSIGNED NOT NULL,
            user_id BIGINT UNSIGNED NOT NULL,
            message TEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES ticket_category(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(user_id) ON DELETE CASCADE
        );
        """
    ]

    # Establish a database connection
    connection = connect_to_database()
    if connection is not None:
        try:
            for statement in sql_statements:
                # Execute each SQL statement
                execute_query(connection, statement)
            print("Database initialized successfully.")
        except mysql.connector.Error as e:
            print(f"Error initializing the database: {e}")
        finally:
            # Ensure that the connection is closed
            if connection.is_connected():
                connection.close()
                print("MySQL connection is closed")
    else:
        print("Failed to connect to the database.")
    
class LoggingConversationHandler(ConversationHandler):
    def check_update(self, update):
        if update.message or update.edited_message:
            result = super().check_update(update)  # Call the original check_update

            # Check for state change
            new_state = result[-1] if result else None
            old_state = self.conversations.get(update.effective_chat.id, {}).get('state')
            if new_state != old_state: 
                print(f"State changed from '{old_state}' to '{new_state}'")
                self.conversations[update.effective_chat.id]['state'] = new_state  # Update stored state

            return result

def main():

    # Initialize Application with your bot's token
    application = Application.builder().token(TOKEN).build()
    # Define the ConversationHandler
    conversation_handler = LoggingConversationHandler(
        entry_points=[CommandHandler('start', start)],
        states={
            DEFAULT_STATE: [
                MessageHandler(filters.ALL & ~filters.COMMAND, handle_all,),
                ],
            IMAGE_PROCESSING: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, process_media,),
                MessageHandler(filters.ALL & ~(filters.COMMAND | filters.PHOTO | filters.Document.IMAGE), handle_non_media),
                               ],
            JOIN_PROCESSING:[
                CallbackQueryHandler(button_click, pattern='joinbtn'),
            ],
            SUBMITTING_TICKET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, submit_ticket),
                
                ],
            ANNOUNCEMENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, capture_announcement_text),
                ],
            BAN_ZONE: [
                
                ],
        },
        fallbacks=[CallbackQueryHandler(button_click, pattern='cancel')]
    )

    # Add ConversationHandler to the application
    application.add_handler(conversation_handler)

    # Add CallbackQueryHandler for button interactions
    application.add_handler(CallbackQueryHandler(button_click,))

    # Set up error handling
    application.add_error_handler(error)
    application.add_handler(CommandHandler("unban", unban_command))

    application.run_polling()

if __name__ == "__main__":
    db_initialize()
    main()
