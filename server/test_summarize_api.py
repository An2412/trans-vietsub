import asyncio
import unittest
from unittest.mock import patch

import google_auth as gauth
import main
import notion_export


class FakeProvider:
    def generate_text(self, prompt, system=""):
        return "## 🎯 Mục tiêu\n- Học FastAPI\n\n## 📝 Nội dung chính\n- Route và dependency"


class FakeTranslator:
    _providers = [("fake", FakeProvider())]

    def translate(self, text, target="vi"):
        return text

    def generate_text(self, prompt, system=""):
        return self._providers[0][1].generate_text(prompt, system)


def run(coro):
    return asyncio.run(coro)


class SummarizeEndpointTests(unittest.TestCase):
    def setUp(self):
        self._translator = patch.object(main, "translator", FakeTranslator())
        self._translator.start()
        self.addCleanup(self._translator.stop)
        # Mặc định coi như đã đăng nhập Google; từng test có thể ghi đè.
        self._auth = patch.object(
            gauth, "get_auth_status", return_value={"connected": True, "email": "a@b.c"}
        )
        self._auth.start()
        self.addCleanup(self._auth.stop)

    def test_summary_without_export(self):
        response = run(main.summarize_endpoint(main.SummarizeReq(
            title="Bài học 1",
            vi_cues=["a", "b", "c"],
            export_to="none",
        )))
        self.assertTrue(response.ok)
        self.assertIn("Mục tiêu", response.summary)
        self.assertEqual(response.target, "none")
        self.assertEqual(response.target_url, "")

    def test_summary_is_cached_by_content(self):
        first = run(main.summarize_endpoint(main.SummarizeReq(
            title="Bài học 1", vi_cues=["a", "b", "c"], export_to="none")))
        self.assertTrue(first.ok)
        # Cùng tiêu đề nhưng nội dung khác → phải tóm tắt lại, không dùng nhầm cache.
        second = run(main.summarize_endpoint(main.SummarizeReq(
            title="Bài học 1", vi_cues=["a", "b", "d"], export_to="none")))
        self.assertTrue(second.ok)

    def test_export_to_notion_creates_child_page(self):
        calls = []

        def fake_request(method, path, token, body=None):
            calls.append((method, path))
            if path == "/pages":
                return {"id": "page-123", "url": "https://notion.so/page-123"}
            return {}

        with patch.object(notion_export, "get_token", return_value="secret_test"), \
             patch.object(notion_export, "get_parent_page_id", return_value="a" * 32), \
             patch.object(notion_export, "_request", side_effect=fake_request):
            response = run(main.summarize_endpoint(main.SummarizeReq(
                title="Bài học 1",
                vi_cues=["a", "b", "c"],
                export_to="notion",
            )))
        self.assertTrue(response.ok)
        self.assertEqual(response.target, "notion")
        self.assertEqual(response.target_id, "page-123")
        self.assertEqual(response.target_url, "https://notion.so/page-123")
        self.assertIn(("POST", "/pages"), calls)

    def test_export_to_notion_appends_when_target_id_given(self):
        calls = []

        def fake_request(method, path, token, body=None):
            calls.append((method, path))
            return {"id": "page-9", "url": "https://notion.so/page-9"}

        with patch.object(notion_export, "get_token", return_value="secret_test"), \
             patch.object(notion_export, "get_parent_page_id", return_value="a" * 32), \
             patch.object(notion_export, "_request", side_effect=fake_request):
            response = run(main.summarize_endpoint(main.SummarizeReq(
                title="Bài học 2",
                vi_cues=["a", "b", "c"],
                export_to="notion",
                target_id="page-9",
            )))
        self.assertTrue(response.ok)
        self.assertTrue(any(path.endswith("/children") for _method, path in calls))

    def test_export_to_gdocs_creates_doc(self):
        calls = []
        bodies = []

        def fake_api_request(method, url, body=None, access_token=""):
            calls.append((method, url))
            bodies.append(body)
            if "documents" in url and method == "POST" and ":batchUpdate" not in url:
                return {"documentId": "doc-1"}
            if "documents" in url and method == "GET":
                # Doc mới tạo: chỉ có thẻ gốc rỗng → hội quy về thẻ 1.
                return {"documentId": "doc-1", "tabs": [{"tabProperties": {"tabId": "t.root", "title": ""}}]}
            if "drive/v3/files" in url and method == "GET":
                # Query tìm folder "Viet Dub Notes"; query tìm doc chủ trả về rỗng.
                # Query dùng quote() nên dấu ' thành %27 — dò qua "google-apps.folder".
                if "google-apps.folder" in url:
                    return {"files": [{"id": "folder-1", "name": "Viet Dub Notes"}]}
                return {"files": []}
            return {}

        with patch.object(gauth, "get_auth_status", return_value={"connected": True, "email": "a@b.c"}), \
             patch.object(gauth, "get_master_doc_id", return_value=""), \
             patch.object(gauth, "save_master_doc_id"):
            with patch.object(gauth, "get_valid_access_token", return_value="tok"), \
              patch.object(gauth, "_api_request", side_effect=fake_api_request):
                response = run(main.summarize_endpoint(main.SummarizeReq(
                    title="Bài học 1",
                    vi_cues=["a", "b", "c"],
                    export_to="gdocs",
                )))
        self.assertTrue(response.ok)
        self.assertEqual(response.target, "gdocs")
        self.assertEqual(response.target_id, "doc-1")
        self.assertIn("docs.google.com", response.target_url)
        # Đổi tên thẻ 1 thành tên video (video đầu tiên chiếm thẻ 1).
        self.assertTrue(any(
            (body or {}).get("requests", [{}])[0].get("updateDocumentTabProperties", {})
            .get("tabProperties", {}).get("title") == "Bài học 1"
            for body in bodies if (body or {}).get("requests")
        ))

    def test_export_to_gdocs_new_tab_per_video(self):
        calls = []
        bodies = []

        def fake_api_request(method, url, body=None, access_token=""):
            calls.append((method, url))
            bodies.append(body)
            if "documents" in url and method == "GET":
                # Sổ tay đã có video 1 ở thẻ gốc (không còn rỗng).
                return {"documentId": "doc-1", "tabs": [{
                    "tabProperties": {"tabId": "t.root", "title": "Bài học 1"},
                    "documentTab": {"body": {"content": [{"startIndex": 0, "endIndex": 42}]}},
                }]}
            if ":batchUpdate" in url and (body or {}).get("requests"):
                first = body["requests"][0]
                if "addDocumentTab" in first:
                    return {"replies": [{"addDocumentTab": {"tabProperties": {"tabId": "t.new"}}}]}
            if "drive/v3/files" in url and method == "GET":
                # Query dùng quote() nên dấu ' thành %27 — dò qua "google-apps.folder".
                if "google-apps.folder" in url:
                    return {"files": [{"id": "folder-1", "name": "Viet Dub Notes"}]}
                return {"files": []}
            return {}

        with patch.object(gauth, "get_auth_status", return_value={"connected": True, "email": "a@b.c"}), \
             patch.object(gauth, "get_master_doc_id", return_value="doc-1"), \
             patch.object(gauth, "get_valid_access_token", return_value="tok"), \
             patch.object(gauth, "_api_request", side_effect=fake_api_request):
            response = run(main.summarize_endpoint(main.SummarizeReq(
                title="Bài học 2",
                vi_cues=["a", "b"],
                export_to="gdocs",
            )))
        self.assertTrue(response.ok)
        batch_bodies = [b for b in bodies if (b or {}).get("requests")]
        tab_requests = [req for body in batch_bodies for req in body["requests"]
                        if "addDocumentTab" in req]
        self.assertTrue(tab_requests, "video mới phải tạo thẻ mới, không ghi đè thẻ video cũ")
        self.assertEqual(
            tab_requests[0]["addDocumentTab"]["tabProperties"]["title"], "Bài học 2"
        )
        # Nội dung phải ghi vào thẻ mới bằng tabId.
        inserts = [req["insertText"]["location"]["tabId"]
                   for body in batch_bodies for req in body["requests"]
                   if "insertText" in req and "location" in req["insertText"]]
        self.assertTrue(all(tab for tab in inserts), "nội dung phải ghi đúng vào thẻ mới")
        self.assertEqual(response.target_id, "doc-1")

    def test_gdocs_re_summarize_same_video_appends_to_its_tab(self):
        calls = []
        bodies = []

        def fake_api_request(method, url, body=None, access_token=""):
            calls.append((method, url))
            bodies.append(body)
            if "documents" in url and method == "GET":
                return {"documentId": "doc-1", "tabs": [{
                    "tabProperties": {"tabId": "t.video1", "title": "Bài học 1"},
                    "documentTab": {"body": {"content": [{"startIndex": 0, "endIndex": 42}]}},
                }]}
            if "drive/v3/files" in url and method == "GET":
                # Query dùng quote() nên dấu ' thành %27 — dò qua "google-apps.folder".
                if "google-apps.folder" in url:
                    return {"files": [{"id": "folder-1", "name": "Viet Dub Notes"}]}
                return {"files": []}
            return {}

        with patch.object(gauth, "get_auth_status", return_value={"connected": True, "email": "a@b.c"}), \
             patch.object(gauth, "get_master_doc_id", return_value="doc-1"), \
             patch.object(gauth, "get_valid_access_token", return_value="tok"), \
             patch.object(gauth, "_api_request", side_effect=fake_api_request):
            response = run(main.summarize_endpoint(main.SummarizeReq(
                title="Bài học 1",
                vi_cues=["a", "b", "c"],
                export_to="gdocs",
            )))
        self.assertTrue(response.ok)
        batch_bodies = [b for b in bodies if (b or {}).get("requests")]
        # Không tạo thẻ mới cho video đã có thẻ.
        tab_requests = [req for body in batch_bodies for req in body["requests"]
                        if "addDocumentTab" in req]
        self.assertFalse(tab_requests)
        # Phải chèn thêm phần "cập nhật" vào thẻ hiện có.
        appends = [req for body in batch_bodies for req in body["requests"]
                   if "insertText" in req and "endOfSegmentLocation" in req["insertText"]]
        self.assertTrue(appends, "tóm tắt lại cùng video phải ghi tiếp vào thẻ đó")
        self.assertIn("Cập nhật", appends[0]["insertText"]["text"])

    def test_summarize_blocked_without_destination(self):
        with patch.object(gauth, "get_auth_status", return_value={"connected": False, "email": ""}), \
             patch.object(notion_export, "get_status", return_value={"configured": False}):
            with self.assertRaises(main.HTTPException) as ctx:
                run(main.summarize_endpoint(main.SummarizeReq(
                    title="Bài học 1", vi_cues=["a", "b", "c"], export_to="none")))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_gdocs_requires_google_even_with_notion_configured(self):
        with patch.object(gauth, "get_auth_status", return_value={"connected": False, "email": ""}), \
             patch.object(notion_export, "get_status", return_value={"configured": True}):
            with self.assertRaises(main.HTTPException) as ctx:
                run(main.summarize_endpoint(main.SummarizeReq(
                    title="Bài học 1", vi_cues=["a", "b", "c"], export_to="gdocs")))
        self.assertEqual(ctx.exception.status_code, 401)

    def test_unknown_export_target_rejected(self):
        with self.assertRaises(main.HTTPException) as ctx:
            run(main.summarize_endpoint(main.SummarizeReq(
                title="Bài học 1", vi_cues=["a", "b", "c"], export_to="dropbox")))
        self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
