.pragma library

var HOURS = [
  { name: "Matins", latin: "Matutinum", command: "prayMatutinum", setting: "matinsTime", defaultTime: "00:00" },
  { name: "Lauds", latin: "Laudes", command: "prayLaudes", setting: "laudsTime", defaultTime: "06:00" },
  { name: "Prime", latin: "Prima", command: "prayPrima", setting: "primeTime", defaultTime: "07:00" },
  { name: "Terce", latin: "Tertia", command: "prayTertia", setting: "terceTime", defaultTime: "09:00" },
  { name: "Sext", latin: "Sexta", command: "praySexta", setting: "sextTime", defaultTime: "12:00" },
  { name: "None", latin: "Nona", command: "prayNona", setting: "noneTime", defaultTime: "15:00" },
  { name: "Vespers", latin: "Vesperae", command: "prayVespera", setting: "vespersTime", defaultTime: "18:00" },
  { name: "Compline", latin: "Completorium", command: "prayCompletorium", setting: "complineTime", defaultTime: "21:00" }
]

var MONTH_SLUGS = [
  "january", "february", "march", "april", "may", "june",
  "july", "august", "september", "october", "november", "december"
]

function parseTime(value, fallback) {
  var match = String(value || "").match(/^(\d{1,2}):(\d{2})$/)
  if (!match) return parseTime(fallback, "00:00")
  var hour = Number(match[1])
  var minute = Number(match[2])
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59)
    return parseTime(fallback, "00:00")
  return hour * 60 + minute
}

function schedule(settings, solarReport) {
  var result = []
  var solarValues = solarReport && !solarReport.error && solarReport.schedule
    ? solarReport.schedule
    : null
  for (var i = 0; i < HOURS.length; i++) {
    var hour = HOURS[i]
    var configured = solarValues && solarValues[hour.setting] !== undefined
      ? solarValues[hour.setting]
      : (settings && settings[hour.setting] !== undefined
        ? settings[hour.setting]
        : hour.defaultTime)
    result.push({
      name: hour.name,
      latin: hour.latin,
      command: hour.command,
      time: String(configured),
      minutes: parseTime(configured, hour.defaultTime),
      solar: solarValues !== null
    })
  }
  result.sort(function(a, b) { return a.minutes - b.minutes })
  return result
}

function currentHourIndex(now, hours) {
  if (!hours || hours.length === 0) return -1
  var currentMinutes = now.getHours() * 60 + now.getMinutes()
  var selected = hours.length - 1
  for (var i = 0; i < hours.length; i++) {
    if (currentMinutes >= hours[i].minutes) selected = i
    else break
  }
  return selected
}

function nextHourIndex(currentIndex, hours) {
  if (!hours || hours.length === 0) return -1
  return (currentIndex + 1) % hours.length
}

function minutesUntilNext(now, currentIndex, hours) {
  if (!hours || hours.length === 0 || currentIndex < 0) return 0
  var next = hours[nextHourIndex(currentIndex, hours)]
  var currentMinutes = now.getHours() * 60 + now.getMinutes()
  var difference = next.minutes - currentMinutes
  if (difference <= 0) difference += 24 * 60
  return difference
}

function remainingLabel(minutes) {
  if (minutes <= 1) return "in 1 min"
  var hours = Math.floor(minutes / 60)
  var rest = minutes % 60
  if (hours === 0) return "in " + rest + " min"
  if (rest === 0) return "in " + hours + " hr"
  return "in " + hours + " hr " + rest + " min"
}

function remainingCompactLabel(minutes) {
  if (minutes <= 1) return "1m left"
  var hours = Math.floor(minutes / 60)
  var rest = minutes % 60
  if (hours === 0) return rest + "m left"
  if (rest === 0) return hours + "h left"
  return hours + "h " + rest + "m left"
}

function pad(value) {
  return value < 10 ? "0" + value : String(value)
}

function isoDate(date) {
  return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate())
}

function siteDate(date) {
  return pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + "-" + date.getFullYear()
}

function ordinalSuffix(value) {
  var remainder100 = value % 100
  if (remainder100 >= 11 && remainder100 <= 13) return "th"
  switch (value % 10) {
  case 1: return "st"
  case 2: return "nd"
  case 3: return "rd"
  default: return "th"
  }
}

function martyrologyUrl(date) {
  var day = date.getDate()
  return "https://catholicsaints.info/roman-martyrology-"
    + MONTH_SLUGS[date.getMonth()] + "-" + day + ordinalSuffix(day) + "/"
}

function query(parameters) {
  var parts = []
  var keys = Object.keys(parameters)
  for (var i = 0; i < keys.length; i++)
    parts.push(encodeURIComponent(keys[i]) + "=" + encodeURIComponent(String(parameters[keys[i]])))
  return parts.join("&")
}

function officeUrl(date, command, version, primaryLanguage, secondaryLanguage) {
  return "https://www.divinumofficium.com/cgi-bin/horas/officium.pl?" + query({
    command: command,
    date1: siteDate(date),
    version: version,
    lang1: primaryLanguage,
    lang2: secondaryLanguage
  })
}

function massUrl(date, version, primaryLanguage, secondaryLanguage) {
  return "https://www.divinumofficium.com/cgi-bin/missa/missa.pl?" + query({
    command: "praySanctaMissa",
    date1: siteDate(date),
    version: version,
    lang1: primaryLanguage,
    lang2: secondaryLanguage
  })
}

function boundedString(value, maximum, allowEmpty) {
  return typeof value === "string"
    && value.length <= maximum
    && (allowEmpty || value.length > 0)
}

function validTime(value) {
  return boundedString(value, 5, false) && /^([01]\d|2[0-3]):[0-5]\d$/.test(value)
}

function validErrorPayload(parsed) {
  return Object.keys(parsed).length === 1
    && boundedString(parsed.error, 1024, false)
}

function validMetadataReport(parsed) {
  if (boundedString(parsed.error, 1024, false) && parsed.title === undefined)
    return validErrorPayload(parsed)
  if (!boundedString(parsed.date, 10, false)
      || !/^\d{4}-\d{2}-\d{2}$/.test(parsed.date)
      || !boundedString(parsed.title, 240, false)
      || !boundedString(parsed.rank, 200, true)
      || !boundedString(parsed.season, 120, true)
      || !boundedString(parsed.sourceUrl, 2048, true)
      || !boundedString(parsed.fetchedAt, 64, true)
      || !boundedString(parsed.error, 1024, true)
      || typeof parsed.stale !== "boolean")
    return false
  if (["", "black", "green", "red", "rose", "violet", "white"].indexOf(parsed.color) < 0)
    return false
  if (!Array.isArray(parsed.commemorations) || parsed.commemorations.length > 4)
    return false
  for (var i = 0; i < parsed.commemorations.length; i++)
    if (!boundedString(parsed.commemorations[i], 240, false)) return false
  if (parsed.requestedDate !== undefined
      && (!boundedString(parsed.requestedDate, 10, false)
        || !/^\d{4}-\d{2}-\d{2}$/.test(parsed.requestedDate)))
    return false
  if (parsed.cooldownUntil !== undefined
      && !boundedString(parsed.cooldownUntil, 64, false))
    return false
  if (parsed.cooldownKind !== undefined
      && ["rate-limit", "access-denied"].indexOf(parsed.cooldownKind) < 0)
    return false
  return true
}

function validSolarReport(parsed) {
  if (!boundedString(parsed.date, 10, false)
      || !/^\d{4}-\d{2}-\d{2}$/.test(parsed.date)
      || !boundedString(parsed.error, 1024, true))
    return false
  if (parsed.error !== "" && parsed.schedule === undefined)
    return parsed.location === undefined || validSolarLocation(parsed.location)
  if (!validSolarLocation(parsed.location)
      || !validTime(parsed.sunrise)
      || !validTime(parsed.sunset)
      || !validTime(parsed.civilDawn)
      || !parsed.schedule
      || typeof parsed.schedule !== "object"
      || Array.isArray(parsed.schedule))
    return false
  var fields = [
    "matinsTime", "laudsTime", "primeTime", "terceTime",
    "sextTime", "noneTime", "vespersTime", "complineTime"
  ]
  for (var i = 0; i < fields.length; i++)
    if (!validTime(parsed.schedule[fields[i]])) return false
  return true
}

function validSolarLocation(location) {
  return location
    && typeof location === "object"
    && !Array.isArray(location)
    && boundedString(location.name, 96, false)
    && typeof location.latitude === "number"
    && isFinite(location.latitude)
    && location.latitude >= -90
    && location.latitude <= 90
    && typeof location.longitude === "number"
    && isFinite(location.longitude)
    && location.longitude >= -180
    && location.longitude <= 180
}

function parseReport(raw, kind) {
  try {
    var parsed = JSON.parse(String(raw || ""))
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null
    if (kind === "solar") return validSolarReport(parsed) ? parsed : null
    return validMetadataReport(parsed) ? parsed : null
  } catch (error) {
    return null
  }
}
