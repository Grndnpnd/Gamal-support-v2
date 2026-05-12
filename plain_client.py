"""
plain_client.py
---------------
Async client for Plain's GraphQL API.
Handles:
  - Upserting customers (identified by Discord user ID as externalId)
  - Creating support threads
  - Replying to threads (customer-side message)
  - Fetching thread timeline (for webhook-less polling fallback)

Required Plain API key permissions:
  customer:create, customer:edit, thread:create, thread:read,
  threadReply:create
"""

import aiohttp
import logging
from typing import Optional

log = logging.getLogger(__name__)

PLAIN_API_URL = "https://core-api.uk.plain.com/graphql/v1"


class PlainClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    async def _gql(self, query: str, variables: dict) -> Optional[dict]:
        payload = {"query": query, "variables": variables}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    PLAIN_API_URL,
                    json=payload,
                    headers=self._headers(),
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    data = await resp.json()
                    if "errors" in data:
                        log.error(f"Plain GraphQL errors: {data['errors']}")
                        return None
                    return data.get("data")
        except Exception as e:
            log.error(f"Plain API request failed: {e}")
            return None

    # ─── Upsert Customer ──────────────────────────────────────────────────────

    async def upsert_customer(
        self,
        discord_user_id: str,
        discord_username: str,
        discord_display_name: str,
    ) -> Optional[str]:
        """
        Upsert a Plain customer using Discord user ID as the externalId.
        Returns the Plain customer ID (c_xxx) or None on failure.
        """
        query = """
        mutation upsertCustomer($input: UpsertCustomerInput!) {
          upsertCustomer(input: $input) {
            customer {
              id
              fullName
              externalId
            }
            error {
              message
              type
              code
              fields { field message type }
            }
          }
        }
        """
        variables = {
            "input": {
                "identifier": {
                    "externalId": f"discord:{discord_user_id}"
                },
                "onCreate": {
                    "fullName": discord_display_name,
                    "externalId": f"discord:{discord_user_id}",
                    "email": {
                        "email": f"discord_{discord_user_id}@discord.invalid",
                        "isVerified": False,
                    },
                },
                "onUpdate": {
                    "fullName": {"value": discord_display_name},
                },
            }
        }

        data = await self._gql(query, variables)
        if not data:
            return None

        result = data.get("upsertCustomer", {})
        if result.get("error"):
            log.error(f"Plain upsertCustomer error: {result['error']}")
            return None

        customer_id = result.get("customer", {}).get("id")
        log.info(f"Plain customer upserted: {customer_id} for Discord user {discord_username}")
        return customer_id

    # ─── Create Thread ────────────────────────────────────────────────────────

    async def create_thread(
        self,
        customer_id: str,
        title: str,
        message_text: str,
        discord_channel_id: str,
        discord_message_id: str,
        discord_username: str,
        label_type_ids: Optional[list] = None,
    ) -> Optional[str]:
        """
        Create a Plain support thread for a customer.
        Stores Discord channel/message IDs in thread fields for webhook routing.
        Returns the Plain thread ID (th_xxx) or None on failure.
        """
        query = """
        mutation createThread($input: CreateThreadInput!) {
          createThread(input: $input) {
            thread {
              id
              externalId
              status
              title
            }
            error {
              message
              type
              code
              fields { field message type }
            }
          }
        }
        """

        components = [
            {
                "componentText": {
                    "text": f"**Discord user:** {discord_username}"
                }
            },
            {
                "componentDivider": {
                    "dividerSpacingSize": "M"
                }
            },
            {
                "componentText": {
                    "text": message_text
                }
            },
            {
                "componentSpacer": {
                    "spacerSize": "M"
                }
            },
            {
                "componentText": {
                    "text": f"_Ticket opened from Discord channel `{discord_channel_id}`_"
                }
            },
        ]

        thread_fields = [
            {
                "key": "discord_channel_id",
                "type": "STRING",
                "stringValue": str(discord_channel_id),
            },
            {
                "key": "discord_message_id",
                "type": "STRING",
                "stringValue": str(discord_message_id),
            },
        ]

        variables = {
            "input": {
                "title": title,
                "customerIdentifier": {"customerId": customer_id},
                "components": components,
                "threadFields": thread_fields,
            }
        }

        if label_type_ids:
            variables["input"]["labelTypeIds"] = label_type_ids

        data = await self._gql(query, variables)
        if not data:
            return None

        result = data.get("createThread", {})
        if result.get("error"):
            log.error(f"Plain createThread error: {result['error']}")
            return None

        thread_id = result.get("thread", {}).get("id")
        log.info(f"Plain thread created: {thread_id}")
        return thread_id

    # ─── Reply to Thread (customer message) ───────────────────────────────────

    async def reply_to_thread(
        self,
        thread_id: str,
        text: str,
    ) -> bool:
        """
        Send a message into an existing Plain thread as the machine user.
        Returns True on success.
        """
        query = """
        mutation replyToThread($input: ReplyToThreadInput!) {
          replyToThread(input: $input) {
            error {
              message
              type
              code
              fields { field message type }
            }
          }
        }
        """
        variables = {
            "input": {
                "threadId": thread_id,
                "textContent": text,
            }
        }

        data = await self._gql(query, variables)
        if not data:
            return False

        result = data.get("replyToThread", {})
        if result.get("error"):
            log.error(f"Plain replyToThread error: {result['error']}")
            return False

        log.info(f"Plain reply sent to thread {thread_id}")
        return True

    # ─── Get Thread Timeline (for polling-based reply delivery) ───────────────

    async def get_thread_messages(self, thread_id: str, after_cursor: Optional[str] = None) -> Optional[dict]:
        """
        Fetch recent timeline entries for a thread.
        Returns a dict with 'messages' (list of {id, body, author_type, cursor})
        and 'end_cursor' for pagination.
        """
        query = """
        query getThreadTimeline($threadId: ID!, $after: String) {
          thread(threadId: $threadId) {
            id
            timelineEntries(first: 20, after: $after) {
              edges {
                cursor
                node {
                  id
                  entry {
                    ... on ChatEntry {
                      text
                      actorType: __typename
                    }
                    ... on EmailEntry {
                      subject
                      textContent
                      actorType: __typename
                    }
                  }
                }
              }
              pageInfo {
                hasNextPage
                endCursor
              }
            }
          }
        }
        """
        variables = {"threadId": thread_id}
        if after_cursor:
            variables["after"] = after_cursor

        data = await self._gql(query, variables)
        if not data:
            return None

        thread = data.get("thread")
        if not thread:
            return None

        entries = thread.get("timelineEntries", {})
        edges = entries.get("edges", [])
        page_info = entries.get("pageInfo", {})

        messages = []
        for edge in edges:
            node = edge.get("node", {})
            entry = node.get("entry", {})
            if not entry:
                continue

            typename = entry.get("actorType", "")
            text = entry.get("text") or entry.get("textContent") or ""

            if text:
                messages.append({
                    "id": node["id"],
                    "text": text,
                    "type": typename,
                    "cursor": edge.get("cursor"),
                })

        return {
            "messages": messages,
            "end_cursor": page_info.get("endCursor"),
            "has_next": page_info.get("hasNextPage", False),
        }
