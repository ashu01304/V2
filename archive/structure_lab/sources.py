"""Demo observations and strictly read-only historical database access."""
from contextlib import contextmanager
from pathlib import Path
import numpy as np
import pandas as pd

ROOT=Path(__file__).resolve().parents[1]
DATABASE=ROOT/'data/master_database.duckdb'


@contextmanager
def read_database(path=DATABASE):
    import duckdb
    path=Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'Database not found: {path}')
    connection=duckdb.connect(str(path),read_only=True,config={
        'memory_limit':'512MB','threads':'2','temp_directory':str(Path(__file__).parent/'runtime/duckdb_temp')})
    try:
        yield connection
    finally:
        connection.close()


def demo_price(product,contract,index):
    """Deterministic synthetic data; never represented as market observations."""
    from .expression import MONTHS
    month=(2000+int(contract[1:]))*12+MONTHS.index(contract[0])
    t=index.astype('int64').to_numpy()/86_400_000_000_000
    base={'CL':75.,'CO':78.,'G':700.,'HO':2.1,'NG':3.,'RB':2.2}[product]
    scale=.01 if product in {'HO','NG','RB'} else .5 if product=='G' else .1
    return base+scale*(.25*(month-2027*12)+np.sin(t/15+month*.17)*3+np.cos(t/5)*.5)


def align_demo(legs,index):
    values=sum(weight*demo_price(product,contract,index) for (product,contract),weight in legs.items())
    return pd.DataFrame({'time':index,'price':values})
