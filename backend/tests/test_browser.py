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
import re
import socket
import time
from threading import Thread

import httpx
import pytest
import uvicorn

from app.main import create_app
from app.roadgraph import build_graph
from tests.osm_factory import grid_city, point
from tests.test_bike import city_with_a_trail
from tests.test_api import FakeGoogle, make_settings
from tests.test_timetable import feed

sync_api = pytest.importorskip("playwright.sync_api")

FROM, TO = point(1, 0), point(2, 2)
TRIP = f"?from={FROM[0]:.5f},{FROM[1]:.5f}&to={TO[0]:.5f},{TO[1]:.5f}"


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    folder = tmp_path_factory.mktemp("site")
    bike_map = folder / "grid-bike.json.gz"
    build_graph(city_with_a_trail().osm.elements, area="grid", mode="bike").save(bike_map)
    walk_map = folder / "grid-walk.json.gz"
    build_graph(grid_city().osm.elements, area="grid", mode="walk").save(walk_map)
    google = FakeGoogle()

    def answer(request: httpx.Request) -> httpx.Response:  # the bus schedule: the GreenLine east from 1 Ave
        return httpx.Response(200, content=feed(point(1, 1))) if request.url.host == "transit.example" else google(request)

    settings = make_settings(folder, map="osm", bike_graph_path=bike_map, walk_graph_path=walk_map, bus_routes_url="https://transit.example/gtfs.zip")
    app = create_app(settings, http=httpx.Client(transport=httpx.MockTransport(answer)))
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


def test_a_map_click_is_named_by_nominatim_where_photon_cant_be_reached(page, site):
    page.route("https://photon.komoot.io/**", lambda route: route.abort())
    nominatim = {"name": "", "address": {"house_number": "4748", "road": "Elizabeth Street", "town": "Caledonia", "state": "Wisconsin"}}
    page.route("https://nominatim.openstreetmap.org/**", lambda route: route.fulfill(json=nominatim))
    open_map(page, site)
    page.mouse.click(*map_spot(page, 0.4, 0.5))
    page.wait_for_function("document.getElementById('origin-picked').textContent.includes('Elizabeth')")
    assert page.locator("#origin-picked").text_content() == "4748 Elizabeth Street, Caledonia, Wisconsin"


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


def test_the_map_at_night_darkens_openstreetmaps_own_tiles(page, site):
    page.clock.set_fixed_time("2026-11-04T06:00:00Z")  # midnight in Milwaukee
    with page.expect_request(lambda request: request.resource_type == "image" and "127.0.0.1" not in request.url) as tile:
        open_map(page, site)
    assert tile.value.url.startswith("https://tile.openstreetmap.org/")
    page.wait_for_selector(".map-tiles--dark", state="attached")  # no tiles here to make it visible
    assert page.evaluate("getComputedStyle(document.querySelector('.map-tiles--dark')).filter").startswith("invert(1)")


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
    page.goto(site + "city.html")  # the 3D city and its library are kept too
    page.wait_for_selector(".jrow")
    assert page.evaluate("typeof THREE.OrbitControls") == "function"
    context.close()


def test_the_city_opens_from_the_planner_and_says_how_the_app_fits_together(page, site):
    open_map(page, site)
    page.click(".chip--city")
    page.wait_for_selector(".jrow")  # with everything from outside blocked: its libraries come with the page
    assert page.locator("h1").text_content() == "Safe Route City" and page.locator(".jrow").count() == 8
    if page.locator("#stage canvas").count() == 0:  # no WebGL in this browser: the same words, as text
        assert page.locator(".nogl").count() == 1
        return
    page.wait_for_selector("#map-osm[aria-pressed='true']")  # it starts on the map this server is set to

    page.click("[data-tour='car-osm']")
    page.wait_for_selector(".step.now")
    assert page.locator("#s-count").text_content() == "Step 1 of 20"
    page.click("#s-play")  # paused, so the steps below stay put
    page.click("#s-next")
    assert page.locator("#s-count").text_content() == "Step 2 of 20"
    assert page.locator(".step.now b").text_content() == "A taxi crosses the river"

    page.click("#tab-places")
    page.click("[data-place='main']")
    assert page.locator("#view h2").text_content() == "Central Station"
    assert page.locator("#view [data-flow='t_route'] b").text_content() == "POST /route"
    page.click("[data-mode='subway']")
    assert page.locator("#view h2").text_content() == "Subway"
    assert page.locator("#view [data-flow='u_price']").count() == 0  # that line only runs on Google's map
    page.click("#map-google")
    page.click("[data-mode='forklift']")
    assert page.locator("#view [data-flow='k_cache']").count() == 1

    page.click(".tool--back")
    page.wait_for_selector("#origin-search input")


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


def test_the_safer_route_game_asks_for_free_uncounted_routes_and_shows_two(page, site):
    asked = route_request(page, lambda: page.goto(site + "game.html" + TRIP))
    assert asked["live_traffic"] is False and asked["counted"] is False
    # The test city's routes may all score the same: then the game says so instead of asking.
    page.wait_for_function(
        "!document.getElementById('pick-a').disabled || /equally safe/.test(document.getElementById('game-status').textContent)"
    )
    if not page.locator("#pick-a").is_disabled():
        with page.expect_request("**/guess"):
            page.click("#pick-a")
        assert page.locator("#game-tally").text_content() == "You: 1 of 1 right" or "0 of 1" in page.locator("#game-tally").text_content()


def test_the_main_screen_opens_the_booths_pages_and_each_leads_back(page, site):
    open_map(page, site)
    for chip, ready in (
        ("game", "#game-trip"),
        ("answers", "#answers-today .tile"),
        ("stats", "#stat-trips"),
        ("control", "#ready li"),
    ):
        page.click(f".chip--{chip}")
        page.wait_for_selector(ready)
        page.click(".chip--back")
        page.wait_for_selector("#origin-search input")


def test_the_answers_page_shows_what_visitors_said(page, site):
    answer = {"safety_gain": 10, "profile": "safe-economy"}
    for choice, extra_min in (("yes", 2), ("yes", 4), ("no", -1.5)):
        assert httpx.post(site + "feedback", json={**answer, "choice": choice, "extra_min": extra_min}).status_code == 204
    page.goto(site + "answers.html")
    today = page.locator("#answers-today")
    today.locator(".answers__table").wait_for()
    assert today.locator(".tile strong").text_content() == "67%" and today.locator(".tile__detail").text_content() == "2 yes · 1 no"
    assert today.locator("tbody tr").nth(0).locator("td").all_text_contents() == ["2", "+10", "+3"]
    assert today.locator("tbody tr").nth(1).locator("td").all_text_contents() == ["1", "+10", "-1.5"]
    assert page.locator("#answers-ever .tile__detail").text_content() == "2 yes · 1 no"


def test_the_booths_pages_read_in_spanish(browser, site):
    context = browser.new_context(locale="es-US", service_workers="block")
    context.route("**/*", lambda route: route.continue_() if "127.0.0.1" in route.request.url else route.abort())
    page = context.new_page()
    open_map(page, site)
    assert page.locator(".chip--answers").text_content() == "Respuestas de los visitantes" and page.locator(".chip--stats").text_content() == "Estadísticas"
    assert page.locator(".chip--city").text_content() == "Por dentro de la app (en inglés)"
    page.click(".chip--answers")
    page.wait_for_selector("#answers-today .tile")
    assert page.locator("h1").text_content() == "Respuestas de los visitantes" and page.locator("#answers-ever .tile span").nth(0).text_content() == "dijeron que tomarían la ruta recomendada"
    assert page.locator(".chip--back").text_content() == "Volver al planificador"
    context.close()


def test_driving_a_car_shows_the_road_from_behind_with_what_comes_up(browser, site):
    context = browser.new_context(
        viewport={"width": 1000, "height": 700}, service_workers="block", permissions=["geolocation"],
        geolocation={"latitude": FROM[0], "longitude": FROM[1]},
    )
    context.route("**/*", lambda route: route.continue_() if "127.0.0.1" in route.request.url else route.abort())
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(error))
    open_map(page, site + TRIP)
    wait_for_routes(page)
    page.click("#drive")
    page.wait_for_selector("body.driving3d", timeout=20000)  # without the map style's server: OpenStreetMap's images
    assert page.locator("#drive-view-map canvas").is_visible() and page.locator(".drive-car").is_visible()
    page.wait_for_selector(".road-sign--turn", state="attached")
    page.click("#drive")
    page.wait_for_function("!document.body.classList.contains('driving3d')")
    assert page.locator("#drive-view").is_hidden()
    context.close()
    assert not errors, errors


def test_the_drive_can_be_previewed_without_a_location_and_ends_by_itself(page, site):
    open_map(page, site + TRIP)
    wait_for_routes(page)
    assert page.locator("#preview").is_visible()
    page.click("#preview")
    page.wait_for_selector("body.driving3d", timeout=20000)
    assert page.locator("#drive").text_content() == "Stop the preview" and page.locator("#drive-preview").is_visible()
    assert page.locator("#preview").is_hidden()
    page.wait_for_function("/ mi left/.test(document.getElementById('nav-banner').textContent)")
    page.wait_for_selector("#drive-road:not([hidden])")
    assert page.locator("#drive-road-name").text_content() == "Main St"
    page.evaluate("import('./core.js').then(({ state }) => { state.nav.preview.rate = 500; })")
    page.wait_for_function("!document.body.classList.contains('driving')", timeout=20000)
    assert page.locator("#nav-banner").text_content() == "That's the end of the preview."
    assert page.locator("#drive").text_content() == "Start driving" and page.locator("#drive-view").is_hidden()
    assert page.locator("#preview").is_visible()


def test_the_previews_speed_can_be_changed_as_it_plays_and_is_remembered(page, site):
    open_map(page, site + TRIP)
    wait_for_routes(page)
    page.click("#preview")
    page.wait_for_selector("body.driving3d", timeout=20000)
    rate = "import('./core.js').then(({ state }) => state.nav.preview.rate)"
    assert page.evaluate(rate) == 3 and page.locator("#drive-preview [data-rate='3']").get_attribute("aria-pressed") == "true"
    page.click("#drive-preview [data-rate='1']")
    assert page.evaluate(rate) == 1
    assert page.locator("#drive-preview [data-rate='1']").get_attribute("aria-pressed") == "true"
    assert page.locator("#drive-preview [data-rate='3']").get_attribute("aria-pressed") == "false"
    page.click("#drive")
    page.wait_for_function("!document.body.classList.contains('driving')")
    page.click("#preview")
    page.wait_for_selector("body.driving3d", timeout=20000)
    assert page.evaluate(rate) == 1


def test_the_compass_says_which_way_the_car_is_going(page, site):
    open_map(page, site + TRIP)
    wait_for_routes(page)
    page.click("#preview")
    page.wait_for_selector("body.driving3d", timeout=20000)
    page.wait_for_function("document.getElementById('drive-compass-point').textContent === 'E'")  # east along Main St
    assert page.locator("#drive-compass").is_visible() and page.locator("#drive-compass").get_attribute("aria-label") == "Going east"
    assert "rotate(-" in page.locator("#drive-compass-dial").get_attribute("style")
    points = page.evaluate("""import('./drive3d.js').then(({ compassPoint }) => [
      [0, 44, 46, 90, 180, 270, 359, -90, 450].map((degrees) => compassPoint(degrees)),
      [compassPoint(24, 0), compassPoint(29, 0), compassPoint(350, 0)],
    ])""")
    assert points[0] == [0, 1, 1, 2, 4, 6, 0, 6, 2]
    assert points[1] == [0, 1, 0]  # past the edge by a little it stays put, and not by more


def test_a_preview_is_for_cars_and_can_be_stopped(page, site):
    open_map(page, site + TRIP)
    wait_for_routes(page)
    page.click("#preview")
    page.wait_for_selector("body.driving3d", timeout=20000)
    page.click("#drive")
    page.wait_for_function("!document.body.classList.contains('driving')")
    assert page.locator("#drive-preview").is_hidden()
    open_map(page, site + TRIP + "&mode=bike")
    wait_for_routes(page)
    assert page.locator("#preview").is_hidden()


def test_the_control_page_reads_in_spanish_down_to_its_messages(browser, site):
    context = browser.new_context(locale="es-US", service_workers="block")
    context.route("**/*", lambda route: route.continue_() if "127.0.0.1" in route.request.url else route.abort())
    page = context.new_page()
    asked = []

    def answer(dialog):
        asked.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", answer)
    page.goto(site + "control.html")
    page.wait_for_selector("#ready li")
    page.wait_for_function("document.getElementById('today').textContent.startsWith('Hoy')")
    assert page.title() == "Control de la expo" and page.locator("h1").text_content() == "Control de la expo"
    assert page.locator("#ready li").first.text_content() == "El servidor · funcionando"
    words = page.locator("main").inner_text()
    english = re.findall(r"\b(?:the|and|Today|trips|Ready|Used|Open|Pause|Restart|Admin|junctions|crossings|schools|requests|of|yes|running|none|not|turned)\b", words)
    assert not english, (english, words)

    page.click("#reset-stats")
    assert asked and asked[0].startswith("¿Contar las estadísticas")
    page.click("#demo-toggle")  # the test's browser is this computer, so the switch answers
    page.wait_for_function("document.getElementById('control-message').textContent === 'Modo de demostración en pausa.'")
    page.wait_for_function("document.getElementById('demo-toggle').textContent === 'Reanudar el modo de demostración'")
    page.click("#demo-toggle")
    page.wait_for_function("document.getElementById('control-message').textContent === 'Modo de demostración reanudado.'")
    context.close()


def test_bike_mode_plans_a_bike_route_colored_by_stress(page, site):
    asked = route_request_to(page, "/bike-route", lambda: open_map(page, site + TRIP + "&mode=bike"))
    assert asked["profile"] == "everyday"
    wait_for_routes(page)
    assert page.evaluate("document.body.dataset.mode") == "bike"
    assert page.locator("#drive").text_content() == "Start riding" and page.locator(".stress-bar").count() >= 1
    assert page.locator("#profile").is_hidden() and page.locator("#bike-profile").is_visible()
    page.click('input[name="mode"][value="car"] + span')  # back to the car: the bike routes go
    assert page.locator(".route").count() == 0 and page.locator("#profile").is_visible()


def route_request_to(page, path, action):
    with page.expect_request(lambda request: request.url.endswith(path) and request.method == "POST") as asked:
        action()
    return asked.value.post_data_json


def test_walking_mode_plans_a_walk_colored_by_sidewalks(page, site):
    asked = route_request_to(page, "/walk-route", lambda: open_map(page, site + TRIP + "&mode=walk"))
    assert asked["profile"] == "everyday"
    wait_for_routes(page)
    assert page.locator("#drive").text_content() == "Start walking" and page.locator(".stress-bar").count() >= 1
    assert page.locator("#walk-profile").is_visible() and page.locator("#bike-profile").is_hidden() and page.locator("#profile").is_hidden()
    with page.expect_request(lambda request: request.url.endswith("/walk-route")) as again:
        page.select_option("#walk-profile", "stepfree")
        page.click("#find")
    assert again.value.post_data_json["profile"] == "stepfree"


def test_transit_mode_shows_bus_trips_step_by_step(page, site):
    deadline = time.monotonic() + 30
    while not httpx.get(site + "health").json()["transit"]["ready"]:  # the schedule is read at startup
        assert time.monotonic() < deadline, "the bus schedule didn't load"
        time.sleep(0.1)

    # Tuesday morning, whenever the test runs: the test schedule's buses run 6 AM to 11 PM.
    def on_tuesday_morning(route):
        route.continue_(post_data={**route.request.post_data_json, "leave_at": "2026-11-10T10:00:00"})

    page.route("**/transit-route", on_tuesday_morning)
    far_east = (point(1, 1)[0], point(1, 1)[1] + 0.0367)  # past the GreenLine's last stop, 3 km on
    trip = f"?from={FROM[0]:.5f},{FROM[1]:.5f}&to={far_east[0]:.5f},{far_east[1]:.5f}&mode=transit"
    route_request_to(page, "/transit-route", lambda: open_map(page, site + trip))
    wait_for_routes(page)
    assert page.locator(".winner .bus-chip").first.text_content() == "GRE"
    assert page.locator("#drive").is_hidden() and page.locator("#add-stop").is_hidden()
    steps = page.locator("#transit-steps .leg")
    assert steps.count() == 3  # walk, ride, arrive: the last stop is at the destination
    assert "Get on at Stop 0 & Main" in page.locator("#transit-steps").text_content()
    assert page.locator(".transit-stop").count() == 2  # where to get on and off
    page.click('input[name="mode"][value="walk"] + span')  # another mode: the bus trip goes
    assert page.locator(".route").count() == 0 and page.locator(".transit-stop").count() == 0
