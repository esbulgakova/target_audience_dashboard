"""Run with: streamlit run frontend/app.py (from the repository root)."""

from datetime import datetime
import os
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st

from frontend.geo import regional_map


API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")
SOURCE_LABELS = {"groups": "Участники сообществ", "likes": "Лайки", "reposts": "Репосты"}
DATA_LABELS = {"demo": "Демонстрация", "vk": "Данные VK"}
AUTH_HEADERS = {"X-Requested-With": "AudienceDashboard"}
COLORS = ["#087F8C", "#F1A66A", "#8A9BAC", "#6CBFB4", "#D2BBEF", "#ACCACD", "#E0E7EC"]


class ApiError(Exception):
    pass


def api_request(method, path, **kwargs):
    try:
        response = requests.request(method, f"{API_BASE_URL}{path}", **kwargs)
    except requests.Timeout:
        raise ApiError("Сбор занял слишком много времени. Повторите поиск позже: готовый результат может уже сохраниться.") from None
    except requests.RequestException:
        raise ApiError("Не удалось связаться с сервисом. Проверьте, запущены ли все контейнеры, и повторите попытку.") from None
    if not response.ok:
        message = None
        try:
            detail = response.json().get("detail")
            if isinstance(detail, str):
                message = detail
        except (ValueError, AttributeError):
            pass
        raise ApiError(message or "Не удалось выполнить запрос. Проверьте тему и выбранные источники, затем повторите поиск.")
    try:
        return response.json()
    except ValueError:
        raise ApiError("Сервис вернул некорректный ответ. Повторите попытку позже.") from None


@st.cache_data(ttl=30, show_spinner=False)
def health():
    return api_request("GET", "/health", timeout=(3, 5))


@st.cache_data(ttl=20, show_spinner=False)
def suggestions(data_mode):
    return api_request("GET", "/api/suggestions", params={"q": "", "data_mode": data_mode},
                       timeout=(3, 5)).get("suggestions", [])


def clear_search():
    st.session_state.pop("dashboard", None)
    st.session_state["search_topic"] = None
    suggestions.clear()


def save_service_token():
    # A widget callback runs before the next render, so its value can be cleared safely.
    secret = st.session_state.get("vk_service_token_input", "").strip()
    try:
        if not secret:
            st.session_state["vk_notice"] = ("warning", "Вставьте сервисный ключ приложения.")
            return
        api_request("POST", "/api/vk/configure", json={"service_token": secret},
                    headers=AUTH_HEADERS, timeout=(3, 20))
        clear_search()
        st.session_state["vk_notice"] = ("success", "Ключ сохранён на сервере. Теперь можно войти через VK ID.")
    except ApiError as error:
        st.session_state["vk_notice"] = ("error", str(error))
    finally:
        st.session_state["vk_service_token_input"] = ""


def disconnect_vk():
    try:
        response = api_request("POST", "/api/vk/disconnect", json={}, headers=AUTH_HEADERS, timeout=(3, 25))
        clear_search()
        st.session_state["vk_notice"] = ("info", response.get("detail") or "Подключение VK отключено.")
    except ApiError as error:
        st.session_state["vk_notice"] = ("error", str(error))


def check_vk_access():
    try:
        response = api_request("POST", "/api/vk/check", json={}, headers=AUTH_HEADERS, timeout=(3, 55))
        level = "success" if response.get("ok") else "warning"
        st.session_state["vk_notice"] = (level, response.get("detail") or "Проверка завершена.")
    except ApiError as error:
        st.session_state["vk_notice"] = ("error", str(error))


def render_vk_connection(status, status_error=None):
    with st.sidebar:
        st.subheader("Подключение VK")
        if status_error:
            st.warning(status_error)
        if status.get("app_id"):
            st.caption(f"Приложение VK ID: {status['app_id']}")
        if status.get("connected"):
            st.success("VK подключён")
            if status.get("user_id"):
                st.caption(f"Аккаунт VK: {status['user_id']}")
            if status.get("source") == "oauth":
                st.caption("Токен обновляется автоматически при обращении к VK.")
            else:
                st.caption("Используется ключ из настроек сервера. Его срок действия нужно проверять вручную.")
            st.button("Проверить доступ к поиску", key="vk_check", on_click=check_vk_access)
            if status.get("source") == "oauth":
                st.button("Отключить VK", key="vk_disconnect", on_click=disconnect_vk)
        elif status.get("reauthorization_required"):
            st.warning("Нужно снова войти через VK ID, чтобы продолжить сбор.")
        elif status.get("configured"):
            st.info("Войдите через VK ID, чтобы подключить реальные данные.")
        elif status.get("enabled"):
            st.info("Откройте настройку ниже и добавьте сервисный ключ приложения.")
        elif not status_error:
            st.caption("Для подключения укажите VK_APP_ID в настройках сервера.")

        if status.get("enabled") and status.get("configured") and status.get("login_url"):
            label = "Войти заново через VK ID" if status.get("connected") else "Войти через VK ID"
            st.link_button(label, status["login_url"], type="primary", width="stretch")
            st.caption("После входа откроется дэшборд. Если остались на этой вкладке, обновите подключение.")
        st.button("Обновить подключение", key="vk_refresh")
        notice = st.session_state.pop("vk_notice", None)
        if notice:
            getattr(st, notice[0])(notice[1])

        if status.get("enabled"):
            with st.expander("Настройка приложения VK ID"):
                st.write("В кабинете VK ID выберите уровень конфиденциальности **«Конфиденциальное»**.")
                st.write(
                    "В поле **«IP-адрес сервера»** укажите внешний IP, с которого этот компьютер "
                    "выходит в интернет. При включённом VPN это может быть IP VPN. "
                    "Адрес localhost в это поле не подходит."
                )
                st.caption("Базовый домен: localhost. Доверенный redirect URL: http://localhost.")
                with st.form("vk_setup"):
                    st.text_input(
                        "Сервисный ключ доступа", type="password", key="vk_service_token_input",
                        help="Скопируйте сервисный ключ из настроек приложения VK ID. Защищённый ключ сюда не подходит.",
                    )
                    st.form_submit_button("Сохранить ключ", on_click=save_service_token)
                st.caption(
                    "Ключ передаётся серверу и очищается из поля. Полученные при входе токены "
                    "хранятся только на сервере; вручную менять их каждый час не нужно."
                )


def number(value):
    return f"{int(value):,}".replace(",", " ")


def timestamp(value):
    if not value:
        return "неизвестно"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M МСК")
    except (ValueError, TypeError):
        return "неизвестно"


def distribution_chart(rows, bars=False):
    labels = [row["label"] for row in rows]
    values = [row["count"] for row in rows]
    percents = [row["percentage"] for row in rows]
    colors = ["#C4CDD5" if "не " in label.casefold() else COLORS[i % len(COLORS)]
              for i, label in enumerate(labels)]
    if bars:
        fig = go.Figure(go.Bar(
            x=labels,
            y=values,
            customdata=percents,
            marker_color=colors,
            text=[number(value) for value in values],
            textposition="outside",
            hovertemplate="%{x}<br>%{y} человек · %{customdata:.1f}%<extra></extra>",
        ))
        fig.update_yaxes(title="Человек", rangemode="tozero", gridcolor="#E9EEF2")
        fig.update_xaxes(type="category", tickangle=0)
    else:
        fig = go.Figure(go.Pie(
            labels=labels,
            values=values,
            customdata=percents,
            marker_colors=colors,
            hole=0.6,
            sort=False,
            textinfo="percent",
            textposition="inside",
            hovertemplate="%{label}<br>%{value} человек · %{customdata:.1f}%<extra></extra>",
        ))
    fig.update_layout(
        height=340,
        margin={"l": 15, "r": 15, "t": 20, "b": 25},
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"family": "sans-serif", "color": "#253449"},
        legend={"orientation": "h", "y": -0.12, "x": 0.5, "xanchor": "center"},
    )
    return fig


def geography_table(rows, total_users, by_city=False):
    columns = ["Страна", "Регион"] + (["Город"] if by_city else [])
    data = [{
        "Страна": row.get("country") or "Не указано",
        "Регион": row.get("region") or "Не указано",
        "Город": row.get("city") or "Не указано",
        "Человек": row["count"],
    } for row in rows]
    if not data:
        return pd.DataFrame(columns=[*columns, "Человек", "Доля, %"])
    frame = pd.DataFrame(data).groupby(columns, dropna=False, as_index=False)["Человек"].sum()
    frame["Доля, %"] = (frame["Человек"] / total_users * 100).round(1) if total_users else 0
    return frame.sort_values("Человек", ascending=False).reset_index(drop=True)


def render_dashboard(result):
    st.divider()
    st.subheader(f"Аудитория: {result['query']}")
    if result["mode"] == "demo":
        st.info("Демонстрация: все пользователи и показатели вымышлены. Они нужны для знакомства с работой дэшборда.", icon="🧪")
    sources = ", ".join(SOURCE_LABELS.get(key, key).lower() for key in result["modes"])
    st.caption(f"Источники: {sources}. Данные собраны {timestamp(result['collected_at'])}.")
    if not result.get("complete", True):
        if result.get("cache_hit"):
            st.warning(
                "Полностью обновить данные не удалось. Ниже показана предыдущая выборка "
                f"от {timestamp(result['collected_at'])}; её срок актуальности истёк. "
                "Повторите поиск позже."
            )
        else:
            st.warning("Сбор завершён частично. Следующий поиск по этой теме снова попробует получить данные.")
    elif result.get("cache_hit"):
        st.caption(f"Использован сохранённый результат. Новый сбор потребуется после {timestamp(result['expires_at'])}.")
    else:
        st.caption(f"Новый сбор завершён. Эти данные можно повторно использовать до {timestamp(result['expires_at'])}.")

    for warning in result.get("warnings", []):
        st.warning(warning)
    if result.get("limited"):
        st.info("Поиск ограничен заданными лимитами. Показатели описывают собранную выборку и не являются оценкой всего ВКонтакте.")

    total = int(result["total_users"])
    coverage = result.get("coverage", {})
    cols = st.columns([1.4, 1, 1, 1])
    with cols[0]:
        st.metric("Уникальных пользователей", number(total), help="Один человек учитывается один раз, даже если найден в нескольких источниках.")
    with cols[1]:
        st.metric("Указан пол", f"{coverage.get('gender', 0):.0f}%")
    with cols[2]:
        st.metric("Известен возраст", f"{coverage.get('age', 0):.0f}%")
    with cols[3]:
        st.metric("Указан город", f"{coverage.get('geography', 0):.0f}%", help="Доля пользователей с указанным городом. На карту попадают только сопоставленные регионы.")

    if not total:
        st.info("В выбранных источниках пользователи не найдены. Попробуйте другую тему или добавьте ещё один сценарий поиска.")
        return

    st.caption("Доли рассчитаны от всей выборки. Пропуски показаны отдельно; скрытые данные не восстанавливаются.")
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.markdown("#### Пол")
            bars = st.toggle("Столбчатая диаграмма", key="gender_bars")
            st.plotly_chart(distribution_chart(result["gender"], bars), use_container_width=True, config={"displayModeBar": False})
    with right:
        with st.container(border=True):
            st.markdown("#### Возраст")
            bars = st.toggle("Столбчатая диаграмма", key="age_bars")
            st.plotly_chart(distribution_chart(result["age"], bars), use_container_width=True, config={"displayModeBar": False})

    with st.container(border=True):
        title, selector = st.columns([2, 1])
        with title:
            st.markdown("#### География аудитории")
        with selector:
            view = st.radio("Вид географии", ["Карта", "Таблица"], horizontal=True, label_visibility="collapsed")
        rows = result.get("geography", [])
        if view == "Карта":
            fig, mapped, unmapped = regional_map(rows, total)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False, "scrollZoom": False})
            st.caption(
                f"На карте: {number(mapped)} человек ({mapped / total * 100:.1f}% выборки). "
                f"Без однозначной привязки к региону: {number(unmapped)}. "
                "Все строки доступны в таблице. Серый цвет означает отсутствие сопоставленных данных."
            )
            st.caption(
                "Схематическая карта регионов СНГ и соседних стран. "
                "[Границы: Natural Earth](https://www.naturalearthdata.com/), public domain. "
                "Название региона сопоставляется со справочником; по одному лишь названию города область не угадывается."
            )
        else:
            by_city = st.toggle("Показать города", key="geo_cities")
            table = geography_table(rows, total, by_city=by_city)
            st.dataframe(table, hide_index=True, width="stretch",
                         column_config={"Доля, %": st.column_config.NumberColumn(format="%.1f %%")})
            st.download_button("Скачать таблицу CSV", table.to_csv(index=False).encode("utf-8-sig"),
                               "audience-geography.csv", "text/csv")
            st.caption("Проценты рассчитаны от всех найденных пользователей, включая неизвестные регионы и страны за пределами карты.")

    with st.expander("Из каких источников собрана выборка"):
        source_rows = [{"Источник": SOURCE_LABELS.get(key, key), "Пользователей": count}
                       for key, count in result.get("sources", {}).items() if key in result["modes"]]
        if source_rows:
            st.dataframe(source_rows, hide_index=True, width="stretch")
        st.caption("Один человек может встретиться в нескольких источниках. Эти числа нельзя складывать для получения размера аудитории.")
        groups = result.get("groups", [])
        if groups:
            st.markdown("**Найденные сообщества**")
            st.dataframe([{
                "Сообщество": group["name"],
                "ID VK": group["id"],
                "Участников по данным VK": group.get("members_count"),
                "Участников в выборке": group.get("collected_count", 0),
            } for group in groups], hide_index=True, width="stretch")


def main():
    st.set_page_config(page_title="Аудитория · VK", page_icon="🔎", layout="wide")
    st.markdown("""
        <style>
        .block-container {max-width: 1240px; padding-top: 2.5rem; padding-bottom: 2rem;}
        [data-testid="stMetric"] {background: #F4F7FA; padding: 1rem 1.2rem; border-radius: 12px;}
        [data-testid="stMetricValue"] {color: #12334A;}
        h1, h2, h3 {color: #15364A;}
        </style>
    """, unsafe_allow_html=True)
    st.caption("УЧЕБНЫЙ ПРОЕКТ  /  АНАЛИТИКА ВКОНТАКТЕ")
    st.title("Найдите аудиторию по теме")
    st.write("Участники сообществ и реакции на публикации — в одном понятном дэшборде.")

    try:
        service = health()
    except ApiError as error:
        st.warning(str(error))
        service = {}
    login_return = st.query_params.pop("vk_login", None)
    if login_return:
        clear_search()
    status_error = None
    try:
        vk_status = api_request("GET", "/api/vk/status", timeout=(3, 5))
    except ApiError as error:
        vk_status = {}
        status_error = str(error)
    connection_identity = (vk_status.get("source"), vk_status.get("user_id"), bool(vk_status.get("connected")))
    if status_error is None and st.session_state.get("vk_connection_identity") != connection_identity:
        clear_search()
        st.session_state["vk_connection_identity"] = connection_identity
    # The URL flag only asks us to refresh. It is never proof of a successful login.
    if login_return and vk_status.get("connected"):
        st.session_state["data_mode"] = "vk"
    render_vk_connection(vk_status, status_error)
    if "data_mode" not in st.session_state:
        st.session_state["data_mode"] = service.get("mode", "demo")
    data_mode = st.radio("Источник данных", list(DATA_LABELS), format_func=DATA_LABELS.get,
                         horizontal=True, key="data_mode", on_change=clear_search)
    search_available = data_mode == "demo" or bool(vk_status.get("connected"))
    if data_mode == "demo":
        st.caption("🧪 Все данные вымышлены · подключение VK не требуется")
    elif status_error:
        st.warning("Подключение VK пока не удалось проверить. Обновите подключение слева перед новым поиском.")
    elif not search_available:
        st.warning("Сначала подключите VK через панель слева. Демонстрация доступна без входа.")
    else:
        st.caption("Сбор доступных данных через API ВКонтакте")
    try:
        previous_queries = suggestions(data_mode) if search_available else []
    except ApiError:
        previous_queries = []

    with st.form("search_form"):
        query = st.selectbox("Тема поиска", previous_queries, index=None, accept_new_options=True, key="search_topic",
                             placeholder="Например: фотография, настольные игры, велоспорт",
                             help="Начните печатать: сохранённые темы появятся в подсказках. Для новой темы нажмите Enter.")
        st.caption("Где искать аудиторию")
        cols = st.columns(3)
        selected = {}
        with cols[0]:
            selected["groups"] = st.checkbox("Участники сообществ", value=True,
                                             help="Сообщества находятся по теме, затем собираются доступные участники.")
        with cols[1]:
            selected["likes"] = st.checkbox("Лайки публикаций", help="Пользователи, поставившие лайк найденным по теме публикациям.")
        with cols[2]:
            selected["reposts"] = st.checkbox("Репосты публикаций", help="Списки репостов доступны только редактору/администратору сообщества или владельцу личной стены.")
        if data_mode == "vk":
            st.caption("Репосты доступны только для стен, которыми вы управляете как редактор, администратор или владелец.")
        submitted = st.form_submit_button("Найти аудиторию", type="primary", width="stretch", disabled=not search_available)

    if submitted and search_available:
        modes = [key for key, value in selected.items() if value]
        if not query or not query.strip():
            st.error("Введите тему поиска.")
        elif not modes:
            st.error("Выберите хотя бы один источник аудитории.")
        else:
            with st.spinner("Собираем доступные данные и готовим статистику. Это может занять несколько минут…"):
                try:
                    result = api_request("POST", "/api/search",
                                         json={"query": query, "modes": modes, "data_mode": data_mode}, timeout=(5, 330))
                    if result.get("mode") != data_mode:
                        raise ApiError("Сервис вернул данные другого режима. Повторите поиск.")
                    st.session_state["dashboard"] = result
                    suggestions.clear()
                except ApiError as error:
                    st.error(str(error))
                    if "dashboard" in st.session_state:
                        st.info("Ниже остался результат предыдущего успешного поиска; тема и время сбора указаны в его заголовке.")

    if st.session_state.get("dashboard", {}).get("mode") == data_mode:
        render_dashboard(st.session_state["dashboard"])
    else:
        st.info("Введите тему и нажмите «Найти аудиторию». Здесь появятся размер выборки, пол, возраст и география.")
        st.caption("Дэшборд показывает доступные публичные данные. Совпадение с темой — признак возможного интереса, а не подтверждённое намерение купить.")


if __name__ == "__main__":
    main()
