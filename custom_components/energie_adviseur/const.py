"""Constanten voor Energie Adviseur."""

DOMAIN = "energie_adviseur"

CONF_KIND = "kind"
KIND_HUB = "hub"
KIND_APPLIANCE = "appliance"
KIND_HEATING = "heating"

# hub
CONF_PRICE_ENTITY = "price_entity"
CONF_PRICE_MULTIPLIER = "price_multiplier"
CONF_USE_ENERGY_DASHBOARD = "use_energy_dashboard"
CONF_SOLAR_ENTITY = "solar_entity"
CONF_BASE_LOAD = "base_load_w"
CONF_EXPORT_DEDUCTION = "export_deduction"
CONF_AI_ENTITY = "ai_entity"
CONF_ADVICE_TIME = "advice_time"

# apparaat
CONF_NAME = "name"
CONF_APPLIANCE_TYPE = "appliance_type"
TYPE_WASHER = "washer"
TYPE_DRYER = "dryer"
TYPE_DISHWASHER = "dishwasher"
TYPE_OTHER = "other"
APPLIANCE_TYPES = [TYPE_WASHER, TYPE_DRYER, TYPE_DISHWASHER, TYPE_OTHER]
CONF_POWER_ENTITY = "power_entity"
CONF_START_W = "start_w"
CONF_STOP_W = "stop_w"
CONF_STOP_DELAY = "stop_delay_min"
CONF_TOLERANCE = "tolerance_pct"
CONF_WINDOW_START = "window_start"
CONF_WINDOW_END = "window_end"
CONF_DRYER_ENTRY = "dryer_entry"

# verwarming
CONF_GAS_ENTITY = "gas_entity"
CONF_INDOOR_ENTITIES = "indoor_entities"
CONF_OUTDOOR_ENTITY = "outdoor_entity"
CONF_AIRCO_ENTITIES = "airco_entities"
CONF_GAS_PRICE = "gas_price"
CONF_GAS_PRICE_ENTITY = "gas_price_entity"
CONF_BOILER_EFF = "boiler_eff_pct"
CONF_COP_7 = "cop_7"
CONF_COP_MINUS7 = "cop_minus7"

EVENT_CYCLE_FINISHED = f"{DOMAIN}_cycle_finished"
MAX_CYCLES = 300
MAX_DAYS = 800
UNAVAILABLE = ("unavailable", "unknown", "", None)


def signal(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_update"
