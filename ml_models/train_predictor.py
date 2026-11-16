# ml_models/train_predictor.py
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, r2_score
from sklearn.model_selection import train_test_split
import pickle
import os

N_SAMPLES = 2000
# Cover the full range the real sensors can report: Random Forests cannot extrapolate,
# so anything outside the training box gets a flat, wrong prediction.
# Keep in sync with MODEL_TEMP_RANGE / MODEL_MOISTURE_RANGE in backend/server.py.
TEMP_RANGE = (0, 45)        # C
MOISTURE_RANGE = (0, 100)   # %

print("Generating synthetic historical data...")
np.random.seed(42)
temperatures = np.random.uniform(*TEMP_RANGE, N_SAMPLES)
moistures = np.random.uniform(*MOISTURE_RANGE, N_SAMPLES)

# The "Rule" the AI will learn: Hotter + Drier = Faster moisture drop (%/hr)
drop_rate = (temperatures * 0.05) + ((100 - moistures) * 0.02) + np.random.normal(0, 0.5, N_SAMPLES)
drop_rate = np.clip(drop_rate, 0, None)  # soil cannot "un-dry" without watering

df = pd.DataFrame({'temperature': temperatures, 'moisture': moistures, 'drop_rate': drop_rate})

X = df[['temperature', 'moisture']]
y = df['drop_rate']
X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

# min_samples_leaf smooths the forest so it learns the trend rather than the noise
model = RandomForestRegressor(n_estimators=100, min_samples_leaf=5, random_state=42)
model.fit(X_train, y_train)

# Evaluate on held-out data
y_pred = model.predict(X_test)
print("[OK] AI Model trained successfully.")
print(f"   Test MAE : {mean_absolute_error(y_test, y_pred):.3f} %/hr")
print(f"   Test R^2 : {r2_score(y_test, y_pred):.3f}")
importance = {c: round(float(v), 3) for c, v in zip(X.columns, model.feature_importances_)}
print(f"   Feature importance: {importance}")

# Refit on all data for the deployed model
model.fit(X, y)

model_path = os.path.join(os.path.dirname(__file__), 'moisture_predictor.pkl')
with open(model_path, 'wb') as f:
    pickle.dump(model, f)
print(f"[OK] Model saved to {model_path}")
