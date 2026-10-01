# model.R - learn which signal characteristics predict winning trades
#
# Run from the project folder:   Rscript model.R
# or open it in RStudio / Positron and run it line by line (best way to learn).
#
# Model: logistic regression with an elastic-net penalty (glmnet).
#
#     log-odds(win) = logit(entry price)            <- offset: the market's own odds, fixed
#                   + b0 + b1*x1 + b2*x2 + ...      <- what the traders add beyond the price
#
# The x's are the RAW measurements saved in the raw_values column (not the 0-1 score
# factors), so the model can find the right shape itself. Each coefficient says how
# much one unit of that variable moves the log-odds of winning beyond the price.
#
# Reads:  history.csv (backfill.py) + signals.csv (hourly script)
# Writes: model_coefficients.json, plots/coefficients.png, plots/calibration.png

suppressPackageStartupMessages({
  library(jsonlite)   # parse the raw_values column (JSON text)
  library(dplyr)      # data wrangling: filter / mutate / group_by / summarise
  library(glmnet)     # regularised regression: shrinks weak variables toward 0
  library(ggplot2)    # charts
})
set.seed(42)          # cross-validation is random; this makes runs repeatable


# ---- 1. load ------------------------------------------------------------------
read_signals <- function(path, source) {
  if (!file.exists(path)) return(NULL)
  d <- read.csv(path, stringsAsFactors = FALSE, fileEncoding = "UTF-8")
  if (!"raw_values" %in% names(d)) return(NULL)
  d |> transmute(date, group, market, side, score = as.numeric(score),
                 entry_price = as.numeric(entry_price), result, raw_values,
                 condition_id, source = source)
}
signals <- bind_rows(read_signals("history.csv", "history"),
                     read_signals("signals.csv", "live")) |>
  filter(result %in% c("won", "lost"), !is.na(entry_price), raw_values != "") |>
  mutate(won = as.integer(result == "won")) |>
  # the same market often appears in OVERALL and in its own category: count it once
  arrange(group == "OVERALL") |>
  distinct(date, condition_id, side, .keep_all = TRUE) |>
  arrange(date)                       # oldest first, so we can test on the newest

cat(nrow(signals), "settled signals,", sum(signals$won), "won\n")
print(table(signals$group))
if (nrow(signals) < 40) stop("Not enough settled signals yet (need 40+, ideally 150+).")


# ---- 2. unpack the raw values into columns -----------------------------------
raw_names <- c("edge_raw", "recent_edge_raw", "coin_flip_rate", "win_rate_raw", "bets",
               "cf_bets", "our_signals", "our_avg_return", "lb_rank", "lb_roi",
               "num_traders", "total_usdc", "opposing_usdc", "price_move_cents",
               "days_to_end", "liquidity_usd")
raw <- lapply(signals$raw_values, fromJSON)
for (k in raw_names) {
  signals[[k]] <- sapply(raw, function(v) if (is.null(v[[k]])) NA_real_ else as.numeric(v[[k]]))
}


# ---- 3. variables for the model ------------------------------------------------
# Missing values: "no history" means no evidence, so edges become 0 and rates the average.
# Leaderboard rank / ROI and liquidity are unknown for backfilled days, so they are
# filled with the average and a 0/1 column `has_live_data` lets the model tell them apart.
fill <- function(x, value) ifelse(is.na(x), value, x)
d <- signals |>
  transmute(
    won, group,
    edge              = fill(edge_raw, 0),                 # wins - expected wins, per bet
    recent_edge       = fill(recent_edge_raw, 0),          # same, last 14 days
    coin_flip_rate    = fill(coin_flip_rate, 0.5),         # win rate on 40-60c bets
    win_rate          = fill(win_rate_raw, mean(win_rate_raw, na.rm = TRUE)),
    log_bets          = log10(fill(bets, 0) + 1),          # experience (10x bets = +1)
    our_avg_return    = fill(our_avg_return, 0),           # their past signals' avg return
    log_our_signals   = log10(fill(our_signals, 0) + 1),
    num_traders,                                           # how many agree
    log_total_usd     = log10(total_usdc),                 # how much money (10x = +1)
    opposing_share    = opposing_usdc / (total_usdc + opposing_usdc),
    price_move_cents  = fill(price_move_cents, 0),         # entry - traders' avg price
    log_days_to_end   = log10(pmax(fill(days_to_end, 1), 0) + 0.1),
    has_live_data     = as.integer(!is.na(lb_rank)),
    lb_rank           = fill(lb_rank, mean(lb_rank, na.rm = TRUE)),
    lb_roi            = fill(lb_roi, mean(lb_roi, na.rm = TRUE)),
    log_liquidity     = fill(log10(liquidity_usd + 1), mean(log10(liquidity_usd + 1), na.rm = TRUE))
  )
d[is.na(d)] <- 0   # if a column is entirely missing (e.g. no live data yet), mean() gives NA

# design matrix: every variable + one column per group + "does agreement matter
# differently per group" (num_traders x group interaction)
X <- model.matrix(won ~ . + num_traders:group - 1, data = d)

p_entry <- pmin(pmax(signals$entry_price, 0.02), 0.98)   # price we could have bought at
offset  <- qlogis(p_entry)                               # the market's own log-odds
y       <- d$won


# ---- 4. honest test: train on older signals, test on the newest 25% ----------
n_test <- ceiling(0.25 * nrow(d))
test   <- (nrow(d) - n_test + 1):nrow(d)
train  <- setdiff(seq_len(nrow(d)), test)

# alpha = 0.5 mixes ridge (shrink everything) and lasso (drop useless variables to 0)
fit <- cv.glmnet(X[train, ], y[train], family = "binomial", offset = offset[train],
                 alpha = 0.5, nfolds = 5)
p_model  <- predict(fit, X[test, ], newoffset = offset[test], s = "lambda.min",
                    type = "response")[, 1]
p_market <- p_entry[test]

log_loss <- function(p, y) -mean(y * log(p) + (1 - y) * log(1 - p))
ret  <- ifelse(y[test] == 1, (1 - p_market) / p_market, -1)   # return per $1 bet
edge <- p_model - p_market
top_score <- signals$score[test] >= quantile(signals$score[test], 0.75, na.rm = TRUE)

cat(sprintf("\nTest on the newest %d signals\n", n_test))
cat(sprintf("  prediction error (log loss, lower = better)\n"))
cat(sprintf("    market price alone : %.4f\n", log_loss(p_market, y[test])))
cat(sprintf("    model              : %.4f\n", log_loss(p_model, y[test])))
cat(sprintf("  average return per $1 if you had bet ...\n"))
cat(sprintf("    on every signal             : %+6.1f%%  (%d)\n", 100 * mean(ret), length(ret)))
cat(sprintf("    on top 25%% by current score : %+6.1f%%  (%d)\n",
            100 * mean(ret[top_score], na.rm = TRUE), sum(top_score, na.rm = TRUE)))
cat(sprintf("    where model edge > 5 points : %+6.1f%%  (%d)\n",
            100 * mean(ret[edge > 0.05]), sum(edge > 0.05)))


# ---- 5. final model on all data, save it --------------------------------------
final <- cv.glmnet(X, y, family = "binomial", offset = offset, alpha = 0.5, nfolds = 5)
coefs <- coef(final, s = "lambda.min")[, 1]
cat("\nCoefficients (0 = dropped; + = raises the odds of winning beyond the price):\n")
print(round(coefs[coefs != 0], 3))

write_json(list(trained = format(Sys.time()), settled_signals = nrow(d),
                intercept = unname(coefs[1]), coefficients = as.list(coefs[-1])),
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

calibration <- data.frame(p = p_model, won = y[test]) |>
  mutate(bin = cut(p, breaks = seq(0, 1, 0.1))) |>
  group_by(bin) |>
  summarise(predicted = mean(p), actual = mean(won), n = n())
ggsave("plots/calibration.png", width = 6, height = 6,
       ggplot(calibration, aes(predicted, actual)) +
         geom_abline(linetype = 2) + geom_point(aes(size = n)) +
         coord_equal(xlim = c(0, 1), ylim = c(0, 1)) +
         labs(title = "Calibration on the test set (dots on the line = honest probabilities)",
              x = "model's predicted win probability", y = "actual win rate"))
cat("\nSaved model_coefficients.json and plots/\n")
