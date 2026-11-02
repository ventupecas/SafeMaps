"""The web map in a real browser: clicks, drags, right-clicks and Undo, against the app on the test city.

The app runs here on a free port with the synthetic grid graph (as in test_api)
and the OpenStreetMap map, so no keys are needed. The map library is served with
the page; map tiles are blocked and the place lookups answered with nothing, so
nothing leaves this computer.

Needs Playwright and a browser (python -m playwright install chromium). Set
ROUTES_TEST_BROWSER=msedge (or chrome) to use an installed browser instead.
Without one, these tests are skipped.
"""

import os
import socket
import time
from threading import Thread

import httpx
import pytest
import uvicorn

from app.main import create_app
from tests.osm_factory import point
from tests.test_api import FakeGoogle, make_settings

sync_api = pytest.importorskip("playwright.sync_api")

FROM, TO = point(1, 0), point(2, 2)
TRIP = f"?from={FROM[0]:.5f},{FROM[1]:.5f}&to={TO[0]:.5f},{TO[1]:.5f}"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    settings = make_settings(tmp_path_factory.mktemp("site"), map="osm")
    app = create_app(settings, http=httpx.Client(transport=httpx.MockTransport(FakeGoogle())))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not server.started:
        assert time.monotonic() < deadline, "the app didn't start"
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/"
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel=os.getenv("ROUTES_TEST_BROWSER") or None)
        except sync_api.Error as exc:
            pytest.skip(f"No browser for the browser tests ({exc.message.splitlines()[0]})")
        yield browser
        browser.close()


@pytest.fixture
def page(browser, site):
    # The offline copy (sw.js) would answer requests out of the routes' sight; it has a test of its own.
    context = browser.new_context(viewport={"width": 1280, "height": 860}, service_workers="block")
    page = context.new_page()

    def network(route):
        host = route.request.url.split("/")[2]
        if host.startswith("127.0.0.1"):
            route.continue_()
        elif host == "photon.komoot.io":
            route.fulfill(json={"type": "FeatureCollection", "features": []})  # no names: the spot's coordinates show
        else:
            route.abort()  # map tiles

    page.route("**/*", network)
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    yield page
    context.close()
    assert not errors, errors


def open_map(page, url):
    page.goto(url)
    page.wait_for_selector("#origin-search input")
    page.evaluate("document.getElementById('legend').open = false")  # the map key, folded off the pins


def center(page, selector):
    box = page.locator(selector).first.bounding_box()
    return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


def map_spot(page, fx, fy):
    box = page.locator("#map").bounding_box()
    return box["x"] + box["width"] * fx, box["y"] + box["height"] * fy


def route_request(page, action):
    """The trip the page asks the server for when action runs."""
    with page.expect_request(lambda request: request.url.endswith("/route") and request.method == "POST") as asked:
        action()
    return asked.value.post_data_json


def wait_for_routes(page):
    """Until the routes are listed and the map has finished zooming to them (the pins stay put)."""
    page.wait_for_selector(".route")
    page.wait_for_function("!document.getElementById('find').classList.contains('is-busy')")
    where = None
    for _ in range(50):
        now = center(page, ".map-pin--destination")
        if now == where:
            return
        where = now
        page.wait_for_timeout(150)


def pins(page):
    return [page.locator(f".map-pin--{kind}").count() for kind in ("origin", "destination", "stop")]


def test_map_clicks_and_removed_pins_are_undone_newest_first(page, site):
    open_map(page, site)
    page.mouse.click(*map_spot(page, 0.4, 0.5))
    page.mouse.click(*map_spot(page, 0.6, 0.5))
    assert pins(page) == [1, 1, 0]
    page.mouse.click(*center(page, ".map-pin--origin"))  # a click on a pin takes it off
    assert pins(page) == [0, 1, 0]
    assert page.locator("#origin-picked").text_content() == ""
    page.keyboard.press("Control+z")
    assert pins(page) == [1, 1, 0]
    page.click("#undo")  # the second click
    page.click("#undo")  # the first
    assert pins(page) == [0, 0, 0] and page.locator("#undo").is_hidden()


def test_a_trip_link_is_planned_with_its_safety_score_explained_and_remembered(page, site):
    asked = route_request(page, lambda: open_map(page, site + TRIP))
    assert (asked["origin"]["lat"], asked["destination"]["lat"]) == (round(FROM[0], 5), round(TO[0], 5))
    wait_for_routes(page)
    assert pins(page) == [1, 1, 0]
    assert page.locator(".winner .breakdown, .winner .breakdown__none").count() == 1
    open_map(page, site)  # back later, with no trip in the link
    assert page.locator("#recent").is_visible() and page.locator(".recent__trip").count() == 1
    page.click("#recent summary")
    route_request(page, lambda: page.click(".recent__trip"))
    wait_for_routes(page)


def test_changes_on_the_map_plan_the_trip_again(page, site):
    open_map(page, site + TRIP)
    wait_for_routes(page)

    def drag_destination():
        x, y = center(page, ".map-pin--destination")
        page.mouse.move(x, y)
        page.mouse.down()
        page.mouse.move(x - 40, y + 10, steps=8)
        page.mouse.up()

    moved = route_request(page, drag_destination)
    assert moved["destination"] != {"lat": round(TO[0], 5), "lng": round(TO[1], 5)}
    wait_for_routes(page)

    (ax, ay), (bx, by) = center(page, ".map-pin--origin"), center(page, ".map-pin--destination")
    with_stop = route_request(page, lambda: page.mouse.click((ax + bx) / 2, (ay + by) / 2 + 30, button="right"))
    assert len(with_stop["stops"]) == 1 and pins(page) == [1, 1, 1]
    wait_for_routes(page)

    swapped = route_request(page, lambda: page.click("#swap"))
    assert swapped["origin"] == with_stop["destination"] and swapped["destination"] == with_stop["origin"]
    wait_for_routes(page)

    undone = route_request(page, lambda: page.keyboard.press("Control+z"))  # takes the stop back off
    assert undone["stops"] == [] and pins(page) == [1, 1, 0]


def test_map_layers_are_remembered(page, site):
    open_map(page, site)
    page.click("#layers summary")
    page.click('input[value="satellite"] + span')
    page.click("#transit-layer + .switch__track")
    page.reload()
    page.wait_for_selector("#origin-search input")
    assert page.locator('input[value="satellite"]').is_checked() and page.locator("#transit-layer").is_checked()


def test_routes_for_a_trip_changed_while_they_were_on_their_way_are_dropped(page, site):
    held = []
    page.route("**/route", lambda route: held.append(route))  # answered only after the trip changes
    open_map(page, site + TRIP)
    for _ in range(100):
        if held:
            break
        page.wait_for_timeout(100)
    page.mouse.click(*center(page, ".map-pin--origin"))  # a pin taken off (at this zoom, A and B overlap)
    held[0].continue_()
    page.wait_for_timeout(1500)
    assert sum(pins(page)) == 1
    assert page.locator(".route").count() == 0 and page.locator("#results .status--error").count() == 0
    assert page.locator("#find").text_content() == "Find route"


def test_the_page_opens_without_the_network_once_it_has_been_seen(browser, site):
    context = browser.new_context()
    context.route("**/*", lambda route: route.continue_() if "127.0.0.1" in route.request.url else route.abort())
    page = context.new_page()
    open_map(page, site)
    page.evaluate("navigator.serviceWorker.ready")  # its copy of the page is kept by now
    context.set_offline(True)
    page.reload()
    page.wait_for_selector("#origin-search input")  # the map library and the server's settings came from the copy
    assert page.locator(".leaflet-container").count() == 1
    context.close()


def test_points_can_be_placed_from_the_keyboard(page, site):
    open_map(page, site + TRIP)
    wait_for_routes(page)
    page.click("#clear")
    page.focus("#map")
    page.keyboard.press("Enter")  # the start, in the middle
    page.keyboard.press("ArrowRight")  # the map moves
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")  # the destination
    assert pins(page) == [1, 1, 0] and page.locator("#map-status").text_content() == "Destination set."
    page.keyboard.press("ArrowUp")
    page.wait_for_timeout(400)
    page.keyboard.press("s")
    assert pins(page) == [1, 1, 1] and page.locator("#map-status").text_content() == "Stop 1 added."
    page.keyboard.press("Control+z")
    assert pins(page) == [1, 1, 0]


def test_the_page_in_spanish_and_back_to_english_with_the_trip_kept(browser, site):
    context = browser.new_context(locale="es-US", service_workers="block")
    context.route("**/*", lambda route: route.continue_() if "127.0.0.1" in route.request.url else route.abort())
    page = context.new_page()
    open_map(page, site + TRIP)
    wait_for_routes(page)
    assert page.locator("html").get_attribute("lang") == "es"
    assert page.locator("#find").text_content() == "Buscar ruta" and page.locator(".winner__badge").text_content() == "Recomendada"
    assert page.locator("#origin-search input").get_attribute("placeholder") == "¿Desde dónde?"
    with page.expect_navigation():
        page.click("#lang")  # English, the same trip
    page.wait_for_selector(".route")
    assert page.locator("#find").text_content() == "Find route" and page.locator("#lang").text_content() == "Español"
    context.close()
