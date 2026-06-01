"""
plain_client.py
---------------
Async client for Plain's GraphQL API.

Two feature groups in one client:

  Ticket support (original):
    - Upserting customers (identified by Discord user ID as externalId)
    - Creating support threads
    - Replying to threads (customer-side message)
    - Fetching thread timeline (for webhook-less polling fallback)

  Help-center article sync (added later):
    - Reading the help center, its categories (article groups), and articles
    - Upserting articles (the customer-facing help.bankr.bot pages)
    Used by the article-sync feature in the admin panel — propose a set of
    article updates from the latest docs, review, then publish to Plain.

Required Plain API key permissions:
  customer:create, customer:edit, thread:create, thread:read,
  threadReply:create,
  helpCenter:read, helpCenterArticleGroup:read,
  helpCenterArticle:read, helpCenterArticle:create, helpCenterArticle:edit
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
            # Pin Accept-Encoding to formats Python decodes natively. aiohttp
            # defaults to including 'br' (Brotli) which requires the optional
            # brotli package — without it, any Brotli-compressed response
            # bombs with "Can not decode content-encoding: br". Avoiding the
            # extra dep is simpler than adding it.
            "Accept-Encoding": "gzip, deflate",
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

    # ──────────────────────────────────────────────────────────────────────────
    # ── Help Center Articles ──────────────────────────────────────────────────
    # ──────────────────────────────────────────────────────────────────────────
    #
    # Used by the article-sync feature in the admin panel. Plain doesn't
    # publicly document the exact help-center mutation shapes, but the API
    # follows clear conventions visible elsewhere in this file:
    #   - mutations take a single $input: <Name>Input! argument
    #   - payloads return the entity + an error { message, type, code, fields }
    #   - resource types are camelCase singular (helpCenter, helpCenterArticle)
    # The field names below match what Plain's MCP skill (plain-docs-sync)
    # documents and what the permission picker uses. If anything turns out to
    # be wrong on first call, _gql logs the GraphQL error verbatim — read the
    # log, adjust the field name here, redeploy.

    async def get_help_center(self, help_center_id: str) -> Optional[dict]:
        """
        Read a single help center by id. Used as the "can we even talk to the
        help-center API" smoke test before any further work.

        Plain's schema:
          - helpCenter(id: ID!) — argument is named `id`
          - HelpCenter has publicName (customer-facing) and internalName
            (admin-facing); no plain `name` field

        Returns the help-center dict on success, None on failure.
        """
        query = """
        query getHelpCenter($id: ID!) {
          helpCenter(id: $id) {
            id
            publicName
            internalName
          }
        }
        """
        data = await self._gql(query, {"id": help_center_id})
        if not data:
            return None
        hc = data.get("helpCenter")
        if not hc:
            log.error(f"Plain getHelpCenter returned no helpCenter for {help_center_id}")
            return None
        log.info(
            f"Plain help center: {hc.get('id')} publicName={hc.get('publicName')!r}"
        )
        return hc

    async def get_help_center_article_groups(self, help_center_id: str) -> Optional[list[dict]]:
        """
        List every article group (category) in the help center, paginating
        through the Connection until exhausted.

        Plain's schema:
          - helpCenter(id: ID!) → HelpCenter
          - HelpCenter.articleGroups returns HelpCenterArticleGroupConnection
            { edges { node { id name slug ... } } pageInfo { hasNextPage endCursor } }
          - So we traverse edges and follow pageInfo.endCursor until done.

        Returns a flat list of group dicts ({ id, name, slug }) — order as
        Plain returns them. None on failure.
        """
        query = """
        query getHelpCenterArticleGroups($id: ID!, $after: String) {
          helpCenter(id: $id) {
            id
            articleGroups(first: 100, after: $after) {
              edges {
                node {
                  id
                  name
                  slug
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
        results: list[dict] = []
        after: Optional[str] = None
        # Cap pagination at a sane number of pages to avoid an unbounded loop
        # if pageInfo ever misbehaves. 100 groups/page × 50 pages = 5000 groups
        # is far more than any real help center has.
        for _ in range(50):
            variables: dict = {"id": help_center_id}
            if after:
                variables["after"] = after
            data = await self._gql(query, variables)
            if not data:
                return None
            hc = data.get("helpCenter")
            if not hc:
                return None
            conn = hc.get("articleGroups") or {}
            for edge in conn.get("edges") or []:
                node = edge.get("node")
                if node:
                    results.append(node)
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
        log.info(f"Plain help center has {len(results)} article group(s)")
        return results

    async def get_help_center_articles(self, help_center_id: str) -> Optional[list[dict]]:
        """
        List every article in the help center with the fields the sync feature
        needs to diff against the docs.

        Plain's schema:
          - HelpCenter.articles returns HelpCenterArticleConnection
          - HelpCenterArticle has: id, title, description, contentHtml,
            slug, status, articleGroup { id, name, slug }, plus timestamps
          - status is an enum (HelpCenterArticleStatus), values include
            PUBLISHED, DRAFT — string-compatible

        Paginates through edges until exhausted. For 22 articles this should
        be one page, but the loop handles growth gracefully.

        Returns a list of article dicts on success, None on failure.
        """
        query = """
        query getHelpCenterArticles($id: ID!, $after: String) {
          helpCenter(id: $id) {
            id
            articles(first: 100, after: $after) {
              edges {
                node {
                  id
                  title
                  description
                  contentHtml
                  slug
                  status
                  articleGroup {
                    id
                    name
                    slug
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
        results: list[dict] = []
        after: Optional[str] = None
        for _ in range(50):
            variables: dict = {"id": help_center_id}
            if after:
                variables["after"] = after
            data = await self._gql(query, variables)
            if not data:
                return None
            hc = data.get("helpCenter")
            if not hc:
                return None
            conn = hc.get("articles") or {}
            for edge in conn.get("edges") or []:
                node = edge.get("node")
                if node:
                    results.append(node)
            page_info = conn.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            after = page_info.get("endCursor")
            if not after:
                break
        log.info(f"Plain help center has {len(results)} article(s)")
        return results

    async def upsert_help_center_article(
        self,
        *,
        help_center_id: str,
        title: str,
        content_html: str,
        article_id: Optional[str] = None,
        article_group_id: Optional[str] = None,
        description: Optional[str] = None,
        icon: Optional[str] = None,
        slug: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Create or update a help center article.

        Plain's real UpsertHelpCenterArticleInput schema (from introspection):
          helpCenterId             ID                       required
          helpCenterArticleId      ID                       optional
          helpCenterArticleGroupId ID                       optional
          title                    String                   required
          description              String                   optional
          icon                     String                   optional
          contentHtml              String                   required
          slug                     String                   optional
          status                   HelpCenterArticleStatus  optional

        Two usage modes:
          1. Update existing — pass article_id to target the article. The
             other optional fields (group/slug/description/status) may be
             omitted; only the fields you pass get updated.
          2. Create new — omit article_id. Pass article_group_id, title,
             content_html. slug, description, status optional but usually
             you want at least slug to set the URL.

        Returns the upserted article dict on success, None on any error.
        GraphQL errors are logged verbatim via _gql for easy debugging.

        Safety: status defaults to None (whatever Plain decides — for an
        update that means "don't change"; for a create that probably means
        DRAFT). Pass status='PUBLISHED' explicitly to publish on upsert.
        """
        query = """
        mutation upsertHelpCenterArticle($input: UpsertHelpCenterArticleInput!) {
          upsertHelpCenterArticle(input: $input) {
            helpCenterArticle {
              id
              slug
              title
              status
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

        # Build the input — only include keys the caller actually provided
        # so we don't accidentally null out fields on an update.
        input_dict: dict = {
            "helpCenterId": help_center_id,
            "title": title,
            "contentHtml": content_html,
        }
        if article_id is not None:
            input_dict["helpCenterArticleId"] = article_id
        if article_group_id is not None:
            input_dict["helpCenterArticleGroupId"] = article_group_id
        if description is not None:
            input_dict["description"] = description
        if icon is not None:
            input_dict["icon"] = icon
        if slug is not None:
            input_dict["slug"] = slug
        if status is not None:
            input_dict["status"] = status

        data = await self._gql(query, {"input": input_dict})
        if not data:
            return None

        result = data.get("upsertHelpCenterArticle", {})
        if result.get("error"):
            log.error(f"Plain upsertHelpCenterArticle error: {result['error']}")
            return None

        article = result.get("helpCenterArticle")
        if not article:
            log.error("Plain upsertHelpCenterArticle returned no article and no error")
            return None
        log.info(
            f"Plain article upserted: {article.get('id')} "
            f"slug='{article.get('slug')}' status={article.get('status')}"
        )
        return article
