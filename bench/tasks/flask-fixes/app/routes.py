"""HTTP routes for the notes service.

Import `app` to serve:  from app.routes import app
"""
from app.server import App, json_response
from app.store import NoteStore

app = App()
store = NoteStore()


@app.route("GET", "/health")
def health(req):
    return json_response({"status": "ok"})


@app.route("GET", "/notes")
def list_notes(req):
    return json_response({"notes": store.list()})


@app.route("POST", "/notes")
def create_note(req):
    data = req.json()
    title = data.get("title")
    if not title:
        return json_response({"error": "title is required"}, 400)
    note = store.add(title, data.get("body", ""), data.get("tags"))
    return json_response({"note": note}, 201)


@app.route("GET", "/notes/<int:note_id>")
def get_note(req, note_id):
    note = store.get(note_id)
    if note is None:
        return json_response({"error": "not found"}, 404)
    return json_response({"note": note})


@app.route("PUT", "/notes/<int:note_id>")
def update_note(req, note_id):
    try:
        data = req.json()
    except ValueError:
        return json_response({"error": "request body must be valid JSON"}, 400)
    note = store.update(note_id, data.get("title"),
                        data.get("body"), data.get("tags"))
    if note is None:
        return json_response({"error": "not found"}, 404)
    return json_response({"note": note})


@app.route("DELETE", "/notes/<int:note_id>")
def delete_note(req, note_id):
    if not store.delete(note_id):
        return json_response({"error": "not found"}, 404)
    return json_response({"deleted": True, "id": note_id})


@app.route("GET", "/notes/search")
def search_notes(req):
    query = req.arg("q", "")
    return json_response({"notes": store.search(query)})
