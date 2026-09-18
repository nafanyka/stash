#!/usr/bin/env python3

import json
import sys
import urllib.request

VR_PATH = "/data/vr/"
VR_TAG = "VR"
NONVR_TAG = "NonVR"


def read_input():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


INPUT = read_input()

server = INPUT.get("server_connection", {})
scheme = server.get("Scheme", "http")
host = server.get("Host", "127.0.0.1")
port = server.get("Port", 9999)
session_cookie = server.get("SessionCookie", {})

GRAPHQL_URL = f"{scheme}://{host}:{port}/graphql"


def graphql(query, variables=None):
    payload = json.dumps({
        "query": query,
        "variables": variables or {}
    }).encode("utf-8")

    req = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={"Content-Type": "application/json"}
    )

    # Authentication passed by Stash to plugin
    if session_cookie:
        if isinstance(session_cookie, dict):
            cookie = "; ".join(f"{k}={v}" for k, v in session_cookie.items())
        else:
            cookie = str(session_cookie)
        req.add_header("Cookie", cookie)

    with urllib.request.urlopen(req) as response:
        result = json.loads(response.read().decode("utf-8"))

    if result.get("errors"):
        raise RuntimeError(result["errors"])

    return result["data"]


def get_or_create_tag(name):
    data = graphql("""
        query FindTags($filter: FindFilterType) {
          findTags(filter: $filter) {
            tags {
              id
              name
            }
          }
        }
    """, {
        "filter": {
            "q": name,
            "per_page": -1
        }
    })

    for tag in data["findTags"]["tags"]:
        if tag["name"].lower() == name.lower():
            return tag["id"]

    data = graphql("""
        mutation TagCreate($input: TagCreateInput!) {
          tagCreate(input: $input) {
            id
          }
        }
    """, {
        "input": {
            "name": name
        }
    })

    return data["tagCreate"]["id"]


def get_scene(scene_id):
    data = graphql("""
        query FindScene($id: ID!) {
          findScene(id: $id) {
            id
            title
            files {
              path
            }
            tags {
              id
              name
            }
          }
        }
    """, {"id": str(scene_id)})

    return data["findScene"]


def classify_scene(scene):
    if not scene:
        return

    paths = [
        f.get("path", "").replace("\\", "/")
        for f in scene.get("files", [])
    ]

    # VR if ANY file belonging to the scene is under /videos/vr/
    is_vr = any(
        path == VR_PATH.rstrip("/") or path.startswith(VR_PATH)
        for path in paths
    )

    wanted_name = VR_TAG if is_vr else NONVR_TAG
    unwanted_name = NONVR_TAG if is_vr else VR_TAG

    wanted_id = get_or_create_tag(wanted_name)

    current_tags = scene.get("tags", [])
    tag_ids = [str(t["id"]) for t in current_tags]

    # Remove opposite classification
    tag_ids = [
        str(t["id"])
        for t in current_tags
        if t["name"].lower() != unwanted_name.lower()
    ]

    # Add required classification
    if str(wanted_id) not in tag_ids:
        tag_ids.append(str(wanted_id))

    graphql("""
        mutation SceneUpdate($input: SceneUpdateInput!) {
          sceneUpdate(input: $input) {
            id
          }
        }
    """, {
        "input": {
            "id": str(scene["id"]),
            "tag_ids": tag_ids
        }
    })

    print(
        f"Path VR Tagger: scene {scene['id']} "
        f"-> {wanted_name} ({', '.join(paths)})",
    )


def process_scene(scene_id):
    classify_scene(get_scene(scene_id))


def process_all():
    page = 1

    while True:
        data = graphql("""
            query FindScenes($filter: FindFilterType) {
              findScenes(filter: $filter) {
                count
                scenes {
                  id
                }
              }
            }
        """, {
            "filter": {
                "page": page,
                "per_page": 100
            }
        })

        scenes = data["findScenes"]["scenes"]

        if not scenes:
            break

        for item in scenes:
            try:
                process_scene(item["id"])
            except Exception as e:
                print(
                    f"Path VR Tagger: scene {item['id']} ERROR: {e}",
                )

        if len(scenes) < 100:
            break

        page += 1


def main():
    args = INPUT.get("args", {})
    mode = args.get("mode")

    # Manual task
    if mode == "process_all":
        process_all()
        return

    # Hook
    hook_context = args.get("hookContext", {})
    scene_id = hook_context.get("id")

    if scene_id:
        process_scene(scene_id)


if __name__ == "__main__":
    main()
