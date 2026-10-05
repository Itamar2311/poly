# patterns.R - daily report: what is actually working?
#
# Run from the project folder:   Rscript patterns.R   (the nightly task runs it after model.R)
#
# Two questions, from real (not backfilled) data:
#   1. Paper trades: which kinds of trades make money? (by category, number of traders,
#      price, edge, exit reason)
#   2. Live snapshots: when top traders take a side, how often does it win compared with
#      what the price said? "realized edge" = actual win rate - average price paid.
#      Positive = following them beats the market; that is the whole idea being tested.
#
# Writes: patterns_report.txt (newest report on top, older ones kept below)
#         plots/patterns_realized_edge.png, plots/patterns_paper_pnl.png

if (dir.exists("r-library")) .libPaths(c(normalizePath("r-library"), .libPaths()))
suppressPackageStartupMessages({
  library(jsonlite)
  library(dplyr)
  library(ggplot2)
})

report <- character()
say <- function(...) report <<- c(report, sprintf(...))
table_text <- function(df) capture.output(print(as.data.frame(df), row.names = FALSE))
band <- function(x, breaks, labels) cut(x, breaks = breaks, labels = labels, include.lowest = TRUE)
price_bands <- function(p) band(p, c(0, .3, .5, .7, .9, 1), c("<30c", "30-50c", "50-70c", "70-90c", "90c+"))

say("==================== %s ====================", format(Sys.time(), "%Y-%m-%d %H:%M"))


# ---- 1. paper trades ---------------------------------------------------------------
paper <- fromJSON("paper_trades.json")$trades |>
  filter(!grepl("cleanup", exit_reason)) |>          # duplicates closed on day 1
  mutate(across(c(entry_price, entry_edge, profit_usd, stake, num_traders), as.numeric),
         settled  = status == "closed",
         exit     = case_when(!settled ~ "still open",
                              grepl("^edge dropped", exit_reason) ~ "edge dropped",
                              grepl("^traders sold", exit_reason) ~ "traders sold",
                              TRUE ~ "market settled"),
         price    = price_bands(entry_price),
         edge     = band(entry_edge, c(-1, .10, .15, .25, 1), c("5-10", "10-15", "15-25", "25+")),
         traders  = ifelse(num_traders >= 3, "3+", as.character(num_traders)))

summarise_paper <- function(df, by) df |>
  group_by(across(all_of(by))) |>
  summarise(trades = n(), closed = sum(settled),
            won = sum(settled & profit_usd > 0),
            profit = round(sum(profit_usd), 2),
            per_dollar = sprintf("%+.0f%%", 100 * sum(profit_usd) / sum(stake)), .groups = "drop")

say("")
say("PAPER TRADES: %d trades, %d closed, profit %+.2f $ on %.0f $ staked (%+.1f%%)",
    nrow(paper), sum(paper$settled), sum(paper$profit_usd), sum(paper$stake),
    100 * sum(paper$profit_usd) / sum(paper$stake))
for (by in list("group", "traders", "price", "edge", "exit")) {
  say("")
  say("-- by %s", by)
  report <- c(report, table_text(summarise_paper(paper, by)))
}


# ---- 2. live snapshots: realized edge ----------------------------------------------
snaps <- read.csv("training.csv", stringsAsFactors = FALSE, fileEncoding = "UTF-8",
                  colClasses = c(condition_id = "character")) |>
  filter(source == "live", result %in% c("won", "lost")) |>
  mutate(won = result == "won",
         traders = ifelse(num_traders >= 3, "3+", as.character(num_traders)),
         price = price_bands(entry_price)) |>
  group_by(date, group, condition_id, side) |>
  mutate(weight = 1 / n()) |>                      # each market position counts once
  ungroup()

# each snapshot is one decision point ("k traders agree at this price"), so count them
realized <- function(df, by) df |>
  group_by(across(all_of(by))) |>
  summarise(snapshots = n(),
            win_rate = round(100 * mean(won)),
            avg_price = round(100 * mean(entry_price)),
            realized_edge = round(100 * (mean(won) - mean(entry_price)), 1),
            .groups = "drop")

say("")
say("LIVE SNAPSHOTS (settled): %d snapshots, %d market positions", nrow(snaps), round(sum(snaps$weight)))
say("realized_edge = win rate - average price paid, in points. > 0 = beat the market.")
if (nrow(snaps) > 0) {
  for (by in list("group", "traders", "price", c("group", "traders"))) {
    say("")
    say("-- by %s", paste(by, collapse = " x "))
    report <- c(report, table_text(realized(snaps, by)))
  }
}


# ---- 3. model over time ---------------------------------------------------------
if (file.exists("model_history.csv")) {
  h <- read.csv("model_history.csv", check.names = FALSE)
  say("")
  say("-- model over the last runs (lower log loss = better; model should beat market)")
  keep <- intersect(c("trained", "live_positions", "backfill_weight", "logloss_market",
                      "logloss_model", "num_traders", "opposing_share", "groupECONOMICS"),
                    names(h))
  report <- c(report, table_text(tail(h[, keep], 7)))
}


# ---- 4. charts + save ---------------------------------------------------------------
dir.create("plots", showWarnings = FALSE)
if (nrow(snaps) > 0) {
  ggsave("plots/patterns_realized_edge.png", width = 7, height = 4.5,
         ggplot(realized(snaps, c("group", "traders")),
                aes(group, realized_edge, fill = traders)) +
           geom_col(position = "dodge") + geom_hline(yintercept = 0) +
           labs(x = NULL, y = "realized edge (points)", fill = "traders",
                title = "Live: win rate minus price paid, by category and traders agreeing"))
}
daily <- fromJSON("paper_trades.json")$daily
if (length(daily) > 0) {
  d <- data.frame(date = as.Date(names(daily)), total = unlist(daily))
  ggsave("plots/patterns_paper_pnl.png", width = 7, height = 4,
         ggplot(d, aes(date, total)) + geom_line() + geom_point() + geom_hline(yintercept = 0) +
           labs(x = NULL, y = "total profit ($)", title = "Paper trading: total profit over time"))
}

old <- if (file.exists("patterns_report.txt")) readLines("patterns_report.txt") else character()
writeLines(c(report, "", "", old), "patterns_report.txt")
cat(report, sep = "\n")
