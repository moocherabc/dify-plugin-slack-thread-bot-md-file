import json
import re
import tempfile
import traceback
import uuid
import requests
import time
from datetime import datetime
from pathlib import Path
from typing import Mapping, List, Tuple, Optional
from werkzeug import Request, Response
from dify_plugin import Endpoint
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError


# ref: https://github.com/fla9ua/markdown_to_mrkdwn
class SlackMarkdownConverter:
    """
    A converter class to transform Markdown text into Slack's mrkdwn format.

    Attributes:
        encoding (str): The character encoding used for the conversion.
        patterns (List[Tuple[str, str]]): A list of regex patterns and their replacements.
    """

    def __init__(self, encoding="utf-8"):
        """
        Initializes the SlackMarkdownConverter with a specified encoding.

        Args:
            encoding (str): The character encoding to use for the conversion. Default is 'utf-8'.
        """
        self.encoding = encoding
        self.in_code_block = False
        self.table_replacements = {}
        # Use compiled regex patterns for better performance
        self.patterns: List[Tuple[re.Pattern, str]] = [
            (
                re.compile(r"^(\s*)- \[([ ])\] (.+)", re.MULTILINE),
                r"\1• ☐ \3",
            ),  # Unchecked task list
            (
                re.compile(r"^(\s*)- \[([xX])\] (.+)", re.MULTILINE),
                r"\1• ☑ \3",
            ),  # Checked task list
            (re.compile(r"^(\s*)- (.+)", re.MULTILINE), r"\1• \2"),  # Unordered list
            (
                re.compile(r"^(\s*)(\d+)\. (.+)", re.MULTILINE),
                r"\1\2. \3",
            ),  # Ordered list
            (re.compile(r"!\[.*?\]\((.+?)\)", re.MULTILINE), r"<\1>"),  # Images to URL
            (
                re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", re.MULTILINE),
                r"_\1_",
            ),  # Italic
            (re.compile(r"^###### (.+)$", re.MULTILINE), r"*\1*"),  # H6 as bold
            (re.compile(r"^##### (.+)$", re.MULTILINE), r"*\1*"),  # H5 as bold
            (re.compile(r"^#### (.+)$", re.MULTILINE), r"*\1*"),  # H4 as bold
            (re.compile(r"^### (.+)$", re.MULTILINE), r"*\1*"),  # H3 as bold
            (re.compile(r"^## (.+)$", re.MULTILINE), r"*\1*"),  # H2 as bold
            (re.compile(r"^# (.+)$", re.MULTILINE), r"*\1*"),  # H1 as bold
            (
                re.compile(r"(^|\s)~\*\*(.+?)\*\*(\s|$)", re.MULTILINE),
                r"\1 *\2* \3",
            ),  # Bold with space handling
            (re.compile(r"(?<!\*)\*\*(.+?)\*\*(?!\*)", re.MULTILINE), r"*\1*"),  # Bold
            (re.compile(r"__(.+?)__", re.MULTILINE), r"*\1*"),  # Underline as bold
            (re.compile(r"\[(.+?)\]\((.+?)\)", re.MULTILINE), r"<\2|\1>"),  # Links
            (re.compile(r"`(.+?)`", re.MULTILINE), r"`\1`"),  # Inline code
            (re.compile(r"^> (.+)", re.MULTILINE), r"> \1"),  # Blockquote
            (
                re.compile(r"^(---|\*\*\*|___)$", re.MULTILINE),
                r"──────────",
            ),  # Horizontal line
            (re.compile(r"~~(.+?)~~", re.MULTILINE), r"~\1~"),  # Strikethrough
        ]
        # Placeholders for triple emphasis
        self.triple_start = "%%BOLDITALIC_START%%"
        self.triple_end = "%%BOLDITALIC_END%%"

    def convert(self, markdown: str) -> str:
        """
        Convert Markdown text to Slack's mrkdwn format.

        Args:
            markdown (str): The Markdown text to convert.

        Returns:
            str: The converted text in Slack's mrkdwn format.
        """
        if not markdown:
            return ""

        try:
            markdown = markdown.strip()

            self.table_replacements = {}

            markdown = self._convert_tables(markdown)

            lines = markdown.split("\n")
            converted_lines = [self._convert_line(line) for line in lines]
            result = "\n".join(converted_lines)

            for placeholder, table in self.table_replacements.items():
                result = result.replace(placeholder, table)

            return result.encode(self.encoding).decode(self.encoding)
        except Exception as e:
            # Log the error for debugging
            return markdown

    def _convert_tables(self, markdown: str) -> str:
        """
        Convert Markdown tables to Slack's mrkdwn format.

        Args:
            markdown (str): The Markdown text containing tables.

        Returns:
            str: The text with tables converted to Slack's format.
        """
        table_pattern = re.compile(
            r"^\|(.+)\|\s*$\n^\|[-:| ]+\|\s*$(\n^\|.+\|\s*$)*", re.MULTILINE
        )

        def convert_table(match):
            original_table = match.group(0)

            table_lines = original_table.strip().split("\n")
            header_line = table_lines[0]
            separator_line = table_lines[1]
            data_lines = table_lines[2:] if len(table_lines) > 2 else []

            headers = [cell.strip() for cell in header_line.strip("|").split("|")]

            rows = []
            for line in data_lines:
                cells = [cell.strip() for cell in line.strip("|").split("|")]
                rows.append(cells)

            result = []
            result.append(" | ".join(f"*{header}*" for header in headers))

            for row in rows:
                result.append(" | ".join(row))

            placeholder = f"%%TABLE_PLACEHOLDER_{hash(original_table)}%%"
            self.table_replacements[placeholder] = "\n".join(result)
            return placeholder

        return table_pattern.sub(convert_table, markdown)

    def _convert_line(self, line: str) -> str:
        """
        Convert a single line of Markdown.

        Args:
            line (str): A single line of Markdown text.

        Returns:
            str: The converted line in Slack's mrkdwn format.
        """
        if line.startswith("%%TABLE_PLACEHOLDER_") and line.endswith("%%"):
            return line

        code_block_match = re.match(r"^```(\w*)$", line)
        if code_block_match:
            language = code_block_match.group(1)
            self.in_code_block = not self.in_code_block
            if self.in_code_block and language:
                return f"```{language}"
            return "```"

        if self.in_code_block:
            return line

        line = re.sub(
            r"(?<!\*)\*\*\*([^*\n]+?)\*\*\*(?!\*)",
            lambda m: f"{self.triple_start}{m.group(1)}{self.triple_end}",
            line,
        )

        for pattern, replacement in self.patterns:
            line = pattern.sub(replacement, line)

        line = re.sub(
            re.escape(self.triple_start) + r"(.*?)" + re.escape(self.triple_end),
            r"*_\1_*",
            line,
            flags=re.MULTILINE,
        )

        return line.rstrip()


class SlackEndpoint(Endpoint):
    CACHE_PREFIX = "thread-cache"
    CACHE_DURATION = 60 * 60 * 24  # 1 day

    def _load_cached_history(self, channel: str, thread_ts: str):
        key = f"{self.CACHE_PREFIX}-{channel}-{thread_ts}"
        try:
            raw = self.session.storage.get(key)
            if raw:
                data = json.loads(raw.decode("utf-8"))
            else:
                return []
        except Exception:
            return []

        now = time.time()
        messages = [m for m in data.get("messages", []) if now - m.get("saved_at", now) < self.CACHE_DURATION]
        if len(messages) != len(data.get("messages", [])):
            data["messages"] = messages
            data["last_cleanup"] = now
            try:
                self.session.storage.set(key, json.dumps(data).encode("utf-8"))
            except Exception:
                pass
        return messages

    def _append_thread_message(self, channel: str, thread_ts: str, message: Mapping):
        key = f"{self.CACHE_PREFIX}-{channel}-{thread_ts}"
        now = time.time()
        try:
            raw = self.session.storage.get(key)
            if raw:
                data = json.loads(raw.decode("utf-8"))
            else:
                data = {"messages": [], "last_cleanup": now}
        except Exception:
            data = {"messages": [], "last_cleanup": now}

        data["messages"] = [m for m in data.get("messages", []) if now - m.get("saved_at", now) < self.CACHE_DURATION]
        msg = dict(message)
        msg["saved_at"] = now
        data["messages"].append(msg)
        data["last_cleanup"] = now
        try:
            self.session.storage.set(key, json.dumps(data).encode("utf-8"))
        except Exception:
            pass

    def _dedupe_sort_messages(self, messages: List[Mapping]) -> List[Mapping]:
        by_ts = {}
        for m in messages:
            ts = m.get("ts")
            if ts is None:
                continue
            by_ts[str(ts)] = m

        def sort_key(m):
            try:
                return float(m.get("ts", 0))
            except (TypeError, ValueError):
                return 0.0

        return sorted(by_ts.values(), key=sort_key)

    def _fetch_conversations_replies(
        self, client: WebClient, channel: str, thread_ts: str
    ) -> List[Mapping]:
        try:
            replies = client.conversations_replies(channel=channel, ts=thread_ts)
            return replies.get("messages", [])
        except SlackApiError as e:
            if e.response.get("error") == "ratelimited":
                retry_after = int(
                    e.response.get("headers", {}).get("Retry-After", 60)
                )
                try:
                    client.chat_postMessage(
                        channel=channel,
                        thread_ts=thread_ts,
                        text=(
                            "Rate limit reached when retrieving thread. "
                            f"Retrying in {retry_after} seconds..."
                        ),
                    )
                except SlackApiError:
                    pass
                time.sleep(retry_after)
                try:
                    replies = client.conversations_replies(
                        channel=channel, ts=thread_ts
                    )
                    return replies.get("messages", [])
                except SlackApiError as retry_err:
                    print(f"Error getting thread history after retry: {retry_err}")
                    return []
            print(f"Error getting thread history: {e}")
            return []

    def _ensure_thread_messages_with_parent(
        self,
        client: WebClient,
        channel: str,
        thread_ts: str,
        messages: List[Mapping],
    ) -> List[Mapping]:
        """
        Ensure the thread parent (ts == thread_ts) is present. Prefer cache to avoid
        Slack rate limits, but fetch conversations.replies when the parent is missing.
        """
        messages = list(messages or [])
        has_parent = any(str(m.get("ts")) == str(thread_ts) for m in messages)
        if messages and has_parent:
            return self._dedupe_sort_messages(messages)

        fetched = self._fetch_conversations_replies(client, channel, thread_ts)
        by_ts = {
            str(m["ts"]): m for m in messages if m.get("ts") is not None
        }
        for m in fetched:
            ts = m.get("ts")
            if ts is None:
                continue
            ts = str(ts)
            if ts not in by_ts:
                by_ts[ts] = m
                self._append_thread_message(channel, thread_ts, m)
            elif ts == str(thread_ts):
                by_ts[ts] = m

        return self._dedupe_sort_messages(list(by_ts.values()))

    def _post_pending_message(
        self, client: WebClient, channel: str, thread_ts: str
    ) -> Optional[str]:
        """Post a temporary 'please wait' notice; returns message ts if successful."""
        try:
            resp = client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text=":hourglass_flowing_sand: 已收到消息，正在努力生成回复…",
            )
            return resp.get("ts")
        except SlackApiError as e:
            print(f"Error posting pending message: {e}")
            return None

    def _clear_pending_message(
        self, client: WebClient, channel: str, pending_ts: Optional[str]
    ):
        if not pending_ts:
            return
        try:
            client.chat_delete(channel=channel, ts=pending_ts)
        except SlackApiError:
            pass

    def _update_pending_message(
        self,
        client: WebClient,
        channel: str,
        pending_ts: Optional[str],
        text: str,
    ):
        if not pending_ts:
            return False
        try:
            client.chat_update(channel=channel, ts=pending_ts, text=text)
            return True
        except SlackApiError:
            return False

    def _upload_answer_as_markdown(
        self,
        client: WebClient,
        channel: str,
        thread_ts: str,
        answer: Optional[str],
        reply_broadcast: bool = False,
    ):
        """
        Write the agent answer to a uniquely named markdown file, upload it to the
        Slack thread, then delete the local file so concurrent sessions cannot collide.
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"dify-reply-{stamp}-{uuid.uuid4().hex[:8]}.md"
        path = Path(tempfile.gettempdir()) / filename
        try:
            path.write_text(answer or "", encoding="utf-8")

            # files_upload_v2 cannot broadcast into the channel; announce once if needed.
            if reply_broadcast:
                client.chat_postMessage(
                    channel=channel,
                    thread_ts=thread_ts,
                    text=f"Reply attached as `{filename}`",
                    reply_broadcast=True,
                )

            return client.files_upload_v2(
                channel=channel,
                thread_ts=thread_ts,
                file=str(path),
                filename=filename,
                title=filename,
            ), filename
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _invoke(self, r: Request, values: Mapping, settings: Mapping) -> Response:
        """
        Invokes the endpoint with the given request.
        """
        # Check if this is a retry and if we should ignore it
        retry_num = r.headers.get("X-Slack-Retry-Num")
        if not settings.get("allow_retry") and (
            r.headers.get("X-Slack-Retry-Reason") == "http_timeout"
            or ((retry_num is not None and int(retry_num) > 0))
        ):
            return Response(status=200, response="ok")

        # Parse the incoming JSON data
        data = r.get_json()

        # Handle Slack URL verification challenge
        if data.get("type") == "url_verification":
            return Response(
                response=json.dumps({"challenge": data.get("challenge")}),
                status=200,
                content_type="application/json",
            )

        # Handle Slack events
        if data.get("type") == "event_callback":
            event = data.get("event")

            # allowed_channel設定を取得
            allowed_channel_setting = settings.get("allowed_channel", "").strip()

            # Handle different event types
            if event.get("type") == "app_mention":
                # Handle mention events - when the bot is @mentioned
                message = event.get("text", "")

                # Remove the bot mention from the beginning of the message
                message = re.sub(r"^<@[^>]+>\s*", "", message)

                # Get channel ID and thread timestamp
                channel = event.get("channel", "")
                # Use thread_ts if the message is in a thread, or use ts to start a new thread
                thread_ts = event.get("thread_ts", event.get("ts"))

                # Process the message and respond
                token = settings.get("bot_token")
                client = WebClient(token=token)

                # store the incoming app mention message
                self._append_thread_message(
                    channel,
                    thread_ts,
                    {
                        "ts": event.get("ts"),
                        "text": event.get("text", ""),
                        "user": event.get("user"),
                        "bot_id": event.get("bot_id"),
                    },
                )

                # allowed_channel が指定されているかチェック
                if allowed_channel_setting:
                    try:
                        # チャンネルIDからチャンネル名を取得
                        channel_info = client.conversations_info(channel=channel)
                        actual_channel_name = channel_info["channel"]["name"]
                        # 取得したチャンネル名に "#" を付けて比較
                        current_channel_with_hash = f"#{actual_channel_name}"
                        if current_channel_with_hash != allowed_channel_setting:
                            # 許可されたチャンネルではなかった場合、メッセージを返して終了
                            client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts,
                                text=(
                                    f"Current channel: {current_channel_with_hash} is not allowed."
                                ),
                            )
                            return Response(
                                status=200, response="ok", content_type="text/plain"
                            )
                    except SlackApiError as e:
                        print(f"Error getting channel info: {e}")
                        try:
                            client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts,
                                text=(
                                    f"Failed to retrieve channel info. SlackApiError: {str(e)}"
                                ),
                            )
                        except SlackApiError:
                            pass
                        return Response(
                            status=200, response="ok", content_type="text/plain"
                        )
                    except Exception as e:
                        print(f"Unexpected error: {e}")
                        try:
                            client.chat_postMessage(
                                channel=channel,
                                thread_ts=thread_ts,
                                text=(
                                    f"An unexpected error occurred while retrieving channel info. Error: {str(e)}"
                                ),
                            )
                        except SlackApiError:
                            pass
                        return Response(
                            status=200, response="ok", content_type="text/plain"
                        )

                pending_ts = None
                try:
                    # Create a key to check if the conversation already exists
                    key_to_check = f"slack-{channel}-{thread_ts}"
                    conversation_id = None
                    try:
                        conversation_id = self.session.storage.get(key_to_check)
                    except Exception as e:
                        err = traceback.format_exc()

                    # Acknowledge early so users see feedback while the agent runs.
                    pending_ts = self._post_pending_message(client, channel, thread_ts)

                    # Get thread history for better context
                    thread_history = []
                    user_id_list = []
                    user_display_name_map = {}
                    # Preserve first-reply broadcast: first Dify turn for this thread.
                    is_first_bot_reply = conversation_id is None
                    if thread_ts:
                        messages = self._ensure_thread_messages_with_parent(
                            client,
                            channel,
                            thread_ts,
                            self._load_cached_history(channel, thread_ts),
                        )

                        # user list in the thread
                        # pattern to extract user id from slack message
                        pattern = r"<@([^>]+)>"
                        # Format messages for context
                        for msg in messages:
                                role = "assistant" if msg.get("bot_id") else "user"
                                content = msg.get("text", "")
                                thread_history.append(
                                    {
                                        "role": role,
                                        "participant_id": msg.get("user", "unknown"),
                                        "content": content,
                                    }
                                )
                                user_id = msg.get("user", "unknown")
                                if user_id != "unknown" and user_id not in user_id_list:
                                    user_id_list.append(user_id)
                                if content != "":
                                    user_ids = re.findall(pattern, content)
                                    for user_id in user_ids:
                                        if user_id not in user_id_list:
                                            user_id_list.append(user_id)


                        # get user display name map from user id list
                        try:
                            for user_id in user_id_list:
                                user_info = client.users_info(user=user_id)
                                user_display_name = user_info.get("user", {}).get(
                                    "name", ""
                                )
                                user_real_name = user_info.get("user", {}).get(
                                    "real_name", ""
                                )
                                if user_display_name != "":
                                    user_display_name_map[user_id] = (
                                        user_real_name + " (" + user_display_name + ")"
                                    )
                                else:
                                    user_display_name_map[user_id] = user_real_name
                        except SlackApiError as e:
                            print(f"Error getting user info: {e}")

                        # add user display name to thread history
                        pattern = r"<@([A-Za-z0-9]+)>"

                        def replace_id_with_name(match):
                            user_id = match.group(1)  # <@...>の...部分を取り出す
                            # user_display_name_mapに存在する場合のみ置換
                            if user_id in user_display_name_map:
                                return f"@{user_display_name_map[user_id]}"
                            else:
                                # 不明なIDの場合はそのままにしておく
                                return match.group(0)

                        for msg in thread_history:
                            msg["participant_name"] = user_display_name_map.get(
                                msg.get("participant_id", "unknown"), "unknown"
                            )
                            msg["content"] = re.sub(
                                pattern, replace_id_with_name, msg["content"]
                            )

                    uploaded_files = []
                    slack_files = event.get("files", [])
                    if slack_files:
                        for f in slack_files:
                            file_name = f.get("name")
                            file_url = f.get("url_private_download")
                            file_mimetype = f.get(
                                "mimetype", "application/octet-stream"
                            )
                            if not file_url or not file_name:
                                continue

                            headers = {"Authorization": f"Bearer {token}"}
                            resp = requests.get(file_url, headers=headers)
                            if resp.status_code == 200:
                                try:
                                    storage_file = self.session.file.upload(
                                        filename=file_name,
                                        content=resp.content,
                                        mimetype=file_mimetype,
                                    )
                                    if storage_file:
                                        uploaded_files.append(storage_file)
                                except Exception as e:
                                    try:
                                        client.chat_postMessage(
                                            channel=channel,
                                            thread_ts=thread_ts,
                                            text=(
                                                f"Error uploading file: {e}\n\n"
                                                "This may be caused by an unconfigured `FILES_URL` in your `dify/docker/.env` .\n"
                                                "Please set `FILES_URL` properly and restart( `docker compose down && docker compose up -d` ) your Dify environment, then try again."
                                            ),
                                        )
                                    except SlackApiError:
                                        pass
                                    print(
                                        f"Error uploading file via session.file.upload: {e}"
                                    )
                            else:
                                print(
                                    f"Failed to download file from Slack: {file_name}, status code={resp.status_code}"
                                )

                    # Invoke the Dify app with the message
                    app_invoke_inputs = {
                        "thread_history": json.dumps(
                            thread_history, indent=4, ensure_ascii=False
                        ),
                        "thread_users": json.dumps(
                            user_display_name_map, indent=4, ensure_ascii=False
                        ),
                        "thread_ts": thread_ts,
                        "channel_id": channel,
                    }
                    if uploaded_files:
                        app_invoke_inputs["files"] = [
                            {
                                "type": uf.type,
                                "transfer_method": "remote_url",
                                "url": uf.preview_url,
                            }
                            for uf in uploaded_files
                        ]

                    invoke_params = {
                        "app_id": settings["app"]["app_id"],
                        "query": re.sub(pattern, replace_id_with_name, message),
                        "inputs": app_invoke_inputs,
                        "response_mode": "blocking",
                    }
                    if conversation_id is not None:
                        invoke_params["conversation_id"] = conversation_id.decode(
                            "utf-8"
                        )

                    response = self.session.app.chat.invoke(**invoke_params)
                    answer = response.get("answer")
                    conversation_id = response.get("conversation_id")
                    if conversation_id:
                        self.session.storage.set(
                            key_to_check, conversation_id.encode("utf-8")
                        )

                    try:
                        reply_broadcast = (
                            settings.get("first_reply_broadcast", False)
                            and is_first_bot_reply
                        )
                        upload_resp, filename = self._upload_answer_as_markdown(
                            client=client,
                            channel=channel,
                            thread_ts=thread_ts,
                            answer=answer,
                            reply_broadcast=reply_broadcast,
                        )
                        files = upload_resp.get("files") or []
                        file_info = files[0] if files else (upload_resp.get("file") or {})
                        self._append_thread_message(
                            channel,
                            thread_ts,
                            {
                                "ts": str(
                                    file_info.get("timestamp")
                                    or file_info.get("created")
                                    or thread_ts
                                ),
                                "text": answer or f"[markdown file: {filename}]",
                                "user": None,
                                "bot_id": "file_upload",
                                "file_id": file_info.get("id"),
                                "filename": filename,
                            },
                        )
                        self._clear_pending_message(client, channel, pending_ts)

                        return Response(
                            status=200, response="ok", content_type="text/plain"
                        )

                    except SlackApiError as e:
                        if not self._update_pending_message(
                            client,
                            channel,
                            pending_ts,
                            f":warning: 发送文件失败：{str(e)}",
                        ):
                            self._clear_pending_message(client, channel, pending_ts)
                        return Response(
                            status=200,
                            response=f"Error sending message to Slack: {str(e)}",
                            content_type="text/plain",
                        )
                except Exception as e:
                    err_msg = str(e)
                    err_trace = traceback.format_exc()

                    skip_timeout_error = settings.get("skip_timeout_error", False)
                    if (
                        skip_timeout_error
                        and "invocation exited without response" in err_msg.lower()
                    ):
                        self._clear_pending_message(client, channel, pending_ts)
                        return Response(
                            status=200,
                            response="ok",
                            content_type="text/plain",
                        )
                    else:
                        error_text = (
                            "Sorry, I'm having trouble processing your request. "
                            f"Please try again later. Error: {err_msg}"
                        )
                        if not self._update_pending_message(
                            client, channel, pending_ts, f":warning: {error_text}"
                        ):
                            try:
                                client.chat_postMessage(
                                    channel=channel,
                                    thread_ts=thread_ts,
                                    text=error_text,
                                )
                            except SlackApiError:
                                pass

                        return Response(
                            status=200,
                            response=f"An error occurred: {err_msg}\n{err_trace}",
                            content_type="text/plain",
                        )
            elif event.get("type") == "message":
                channel = event.get("channel", "")
                thread_ts = event.get("thread_ts") or event.get("ts")
                recognized = False
                key_to_check = f"slack-{channel}-{thread_ts}"
                try:
                    if self.session.storage.get(key_to_check):
                        recognized = True
                except Exception:
                    pass
                if not recognized:
                    try:
                        if self.session.storage.get(
                            f"{self.CACHE_PREFIX}-{channel}-{thread_ts}"
                        ):
                            recognized = True
                    except Exception:
                        pass
                if recognized:
                    self._append_thread_message(
                        channel,
                        thread_ts,
                        {
                            "ts": event.get("ts"),
                            "text": event.get("text", ""),
                            "user": event.get("user"),
                            "bot_id": event.get("bot_id"),
                        },
                    )
                return Response(status=200, response="ok")
            else:
                # Other event types we're not handling
                return Response(status=200, response="ok")
        else:
            # Not an event we're handling
            return Response(status=200, response="ok")
