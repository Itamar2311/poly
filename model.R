# model.R - learn which top-trader positions win more often than the price says
#
# Run from the project folder:   Rscript model.R
# or open it in RStudio / Positron and run it line by line (best way to learn).
#
# Model: logistic regression with an elastic-net penalty (glmnet).
#
#     log-odds(win) = logit(entry price)            <- offset: the market's own odds, fixed
#                   + b0 + b1*x1 + b2*x2 + ...      <- what the traders add beyond the price
#
# Data: training.csv - one row ("snapshot") every time one more top trader of a group
# took the same side of a market: the 1st trader alone, then 2 agreeing, 3, ... each
# with the price at that moment. So the model sees single-trader bets too and can learn
# what each extra trader adds. A market that reached 3 traders has 3 rows; each gets
# weight 1/3 so every market counts once. Backfilled history is phased out as live
# data builds up (see BACKFILL_GONE_AT).
#
# The x's are the RAW measurements in the raw_values column. Each coefficient says how
# much one unit of that variable moves the log-odds of winning beyond the price.
#
# Writes: model_coefficients.json, plots/coefficients.png, plots/calibration.png

# packages live in the project's r-library folder, so every way of running this script
# (RStudio, Rscript, the nightly scheduled task) finds the same ones. To set it up:
#   install.packages(c("jsonlite","glmnet","dplyr","ggplot2"), lib = "r-library")
if (dir.exists("r-library")) .libPaths(c(normalizePath("r-library"), .libPaths()))

suppressPackageStartupMessages({
  library(jsonlite)   # parse the raw_values column (JSON text)
  library(dplyr)      # data wrangling: filter / mutate / group_by / summarise
  library(glmnet)     # regularised regression: shrinks weak variables toward 0
  library(ggplot2)    # charts
})
set.seed(42)          # cross-validation is random; this makes runs repeatable


# ---- 1. load ------------------------------------------------------------------
# condition_id looks like "0x12ab..." - without colClasses R reads it as a hex number
snaps <- read.csv("training.csv", stringsAsFactors = FALSE, fileEncoding = "UTF-8",
                  colClasses = c(condition_id = "character")) |>
  filter(result %in% c("won", "lost"), !is.na(entry_price), raw_values != "") |>
  mutate(won = as.integer(result == "won"))

# OVERALL traders' markets usually also show up in their own category: keep the category
category_keys <- snaps |> filter(group != "OVERALL") |> distinct(date, condition_id, side)
snaps <- bind_rows(snaps |> filter(group != "OVERALL"),
                   snaps |> filter(group == "OVERALL") |>
                     anti_join(category_keys, by = c("date", "condition_id", "side")))

# every market-side-day counts once in total, however many snapshots it has
snaps <- snaps |>
  group_by(date, group, condition_id, side) |>
  mutate(weight = 1 / n()) |>
  ungroup() |>
  arrange(snapshot_time)              # oldest first, so we can test on the newest

# Phase out the backfill: it only contains today's top traders, so it looks better than
# reality. Full weight while live data is scarce, half at 1000 settled live positions,
# gone at 2000 - the model moves over to clean live data on its own.
BACKFILL_GONE_AT <- 2000
live_positions   <- sum(snaps$weight[snaps$source == "live"])
backfill_weight  <- max(0, 1 - live_positions / BACKFILL_GONE_AT)
snaps <- snaps |>
  mutate(weight = ifelse(source == "backfill", weight * backfill_weight, weight)) |>
  filter(weight > 0)
cat(sprintf("live positions: %.0f -> backfill weight %.2f\n", live_positions, backfill_weight))

cat(nrow(snaps), "settled snapshots from", round(sum(snaps$weight)), "market positions,",
    round(100 * mean(snaps$won)), "% won\n")
print(table(group = snaps$group, traders = pmin(snaps$num_traders, 4)))
if (nrow(snaps) < 100) stop("Not enough settled snapshots yet.")


# ---- 2. unpack the raw values into columns -----------------------------------
# all rows' JSON in one go: '[{...},{...}]' -> a data frame (much faster than row by row)
raw <- fromJSON(paste0("[", paste(snaps$raw_values, collapse = ","), "]"))
raw_names <- c("edge_raw", "recent_edge_raw", "coin_flip_rate", "win_rate_raw", "bets",
               "our_signals", "our_avg_return", "lb_rank", "lb_roi", "total_usdc",
               "opposing_usdc", "price_move_cents", "days_to_end", "liquidity_usd")
for (k in raw_names) snaps[[k]] <- if (k %in% names(raw)) as.numeric(raw[[k]]) else NA_real_


# ---- 3. variables for the model ------------------------------------------------
# Missing values: "no history" means no evidence, so edges become 0 and rates the average.
# Leaderboard rank / ROI and liquidity are unknown for backfilled days, so they are
# filled with the average and a 0/1 column `has_live_data` lets the model tell them apart.
fill <- function(x, value) ifelse(is.na(x), value, x)
avg  <- function(x) { m <- mean(x, na.rm = TRUE); if (is.nan(m)) 0 else m }
# averages used to fill gaps - saved with the model so Python fills gaps the same way
fills <- list(win_rate      = avg(snaps$win_rate_raw),
              lb_rank       = avg(snaps$lb_rank),
              lb_roi        = avg(snaps$lb_roi),
              log_liquidity = avg(log10(snaps$liquidity_usd + 1)))

d <- snaps |>
  transmute(
    won, group,
    edge              = fill(edge_raw, 0),                 # wins - expected wins, per bet
    recent_edge       = fill(recent_edge_raw, 0),          # same, last 14 days
    coin_flip_rate    = fill(coin_flip_rate, 0.5),         # win rate on 40-60c bets
    win_rate          = fill(win_rate_raw, fills$win_rate),
    log_bets          = log10(fill(bets, 0) + 1),          # experience (10x bets = +1)
    our_avg_return    = fill(our_avg_return, 0),           # their past signals' avg return
    log_our_signals   = log10(fill(our_signals, 0) + 1),
    num_traders,                                           # how many agree (1, 2, 3, ...)
    two_plus          = as.integer(num_traders >= 2),      # jump from alone -> agreement
    log_total_usd     = log10(pmax(total_usdc, 1)),        # how much money (10x = +1)
    opposing_share    = opposing_usdc / (total_usdc + opposing_usdc),
    price_move_cents  = fill(price_move_cents, 0),         # entry - traders' avg price
    log_days_to_end   = log10(pmax(fill(days_to_end, 1), 0) + 0.1),
    has_live_data     = as.integer(!is.na(lb_rank)),
    lb_rank           = fill(lb_rank, fills$lb_rank),
    lb_roi            = fill(lb_roi, fills$lb_roi),
    log_liquidity     = fill(log10(liquidity_usd + 1), fills$log_liquidity)
  )
d[is.na(d)] <- 0
# Leaderboard rank / ROI and liquidity only exist in live data. With few settled live
# positions their coefficients are pure noise, so they stay out of the model until then.
LIVE_VARIABLES_FROM <- 500
if (live_positions < LIVE_VARIABLES_FROM) {
  d <- d |> mutate(has_live_data = 0, lb_rank = 0, lb_roi = 0, log_liquidity = 0)
  cat(sprintf("live-only variables left out until %d live positions\n", LIVE_VARIABLES_FROM))
}
# (polymarket_signals.py -> model_features() rebuilds exactly these columns in Python)

# design matrix: every variable + one column per group + "does agreement matter
# differently per group" (num_traders x group interaction)
X <- model.matrix(won ~ . + num_traders:group - 1, data = d)

p_entry <- pmin(pmax(snaps$entry_price, 0.02), 0.98)   # price at the snapshot moment
offset  <- qlogis(p_entry)                             # the market's own log-odds
y       <- d$won
w       <- snaps$weight


# ---- 4. honest test: train on older snapshots, test on the newest 25% --------
n_test <- ceiling(0.25 * nrow(d))
test   <- (nrow(d) - n_test + 1):nrow(d)
train  <- setdiff(seq_len(nrow(d)), test)

# alpha = 0.5 mixes ridge (shrink everything) and lasso (drop useless variables to 0)
fit <- cv.glmnet(X[train, ], y[train], weights = w[train], family = "binomial",
                 offset = offset[train], alpha = 0.5, nfolds = 5)
p_model  <- predict(fit, X[test, ], newoffset = offset[test], s = "lambda.min",
                    type = "response")[, 1]
p_market <- p_entry[test]

wmean    <- function(x, wt) sum(x * wt) / sum(wt)
log_loss <- function(p, y, wt) -wmean(y * log(p) + (1 - y) * log(1 - p), wt)
ret  <- ifelse(y[test] == 1, (1 - p_market) / p_market, -1)   # return per $1 bet
edge <- p_model - p_market
wt   <- w[test]

cat(sprintf("\nTest on the newest %d snapshots\n", n_test))
cat(sprintf("  prediction error (log loss, lower = better)\n"))
cat(sprintf("    market price alone : %.4f\n", log_loss(p_market, y[test], wt)))
cat(sprintf("    model              : %.4f\n", log_loss(p_model, y[test], wt)))
cat("  average return per $1 if you had bet ...\n")
show <- function(label, keep) cat(sprintf("    %-34s %+6.1f%%  (%d)\n", label,
                                         100 * wmean(ret[keep], wt[keep]), sum(keep)))
show("on every snapshot", rep(TRUE, n_test))
for (k in 1:3) show(sprintf("when %d trader%s agreed", k, if (k > 1) "s" else ""),
                    pmin(d$num_traders[test], 3) == k)
show("where model edge > 5 points", edge > 0.05)
show("2+ traders and edge > 5 points", edge > 0.05 & d$num_traders[test] >= 2)


# ---- 5. final model on all data, save it --------------------------------------
final <- cv.glmnet(X, y, weights = w, family = "binomial", offset = offset,
                   alpha = 0.5, nfolds = 5)
coefs <- coef(final, s = "lambda.min")[, 1]
cat("\nCoefficients (0 = dropped; + = raises the odds of winning beyond the price):\n")
print(round(coefs[coefs != 0], 3))

# one row per run in model_history.csv: test results + every coefficient, so you can
# see over the weeks which effects are stable and which drift
history_row <- data.frame(
  trained = format(Sys.time(), "%Y-%m-%d %H:%M"), snapshots = nrow(d),
  live_positions = round(live_positions), backfill_weight = round(backfill_weight, 3),
  logloss_market = round(log_loss(p_market, y[test], wt), 4),
  logloss_model = round(log_loss(p_model, y[test], wt), 4),
  return_all = round(wmean(ret, wt), 4),
  return_edge5 = round(wmean(ret[edge > 0.05], wt[edge > 0.05]), 4),
  t(round(coefs, 4)), check.names = FALSE)
if (file.exists("model_history.csv")) {
  old <- read.csv("model_history.csv", check.names = FALSE)
  history_row <- bind_rows(old, history_row)       # new coefficients add new columns
}
write.csv(history_row, "model_history.csv", row.names = FALSE, na = "")

write_json(list(trained = format(Sys.time()), snapshots = nrow(d),
                intercept = unname(coefs[1]), coefficients = as.list(coefs[-1]),
                fills = fills),
           "model_coefficients.json", auto_unbox = TRUE, pretty = TRUE, digits = 6)


# ---- 6. charts -------------------------------------------------------------------
dir.create("plots", showWarnings = FALSE)

coef_df <- data.frame(variable = names(coefs)[-1], effect = coefs[-1]) |> filter(effect != 0)
if (nrow(coef_df) > 0) {
  ggsave("plots/coefficients.png", width = 7, height = 5,
         ggplot(coef_df, aes(reorder(variable, effect), effect, fill = effect > 0)) +
           geom_col(show.legend = FALSE) + coord_flip() +
           labs(x = NULL, y = "effect on log-odds of winning (beyond the price), per unit",
                title = "What moves the odds beyond the market price"))
}

calibration <- data.frame(p = p_model, won = y[test], wt = wt) |>
  mutate(bin = cut(p, breaks = seq(0, 1, 0.1))) |>
  group_by(bin) |>
  summarise(predicted = wmean(p, wt), actual = wmean(won, wt), n = n())
ggsave("plots/calibration.png", width = 6, height = 6,
       ggplot(calibration, aes(predicted, actual)) +
         geom_abline(linetype = 2) + geom_point(aes(size = n)) +
         coord_equal(xlim = c(0, 1), ylim = c(0, 1)) +
         labs(title = "Calibration on the test set (dots on the line = honest probabilities)",
              x = "model's predicted win probability", y = "actual win rate"))
cat("\nSaved model_coefficients.json and plots/\n")
