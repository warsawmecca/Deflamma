"""Percentile harmonic score with positive population mean-deviation boost."""
import numpy as np

def midpoint_percentiles(values):
    """Fraction below + half of ties, over all valid paired land pixels."""
    values=np.asarray(values,dtype='f8')
    if values.ndim!=1 or not len(values) or not np.isfinite(values).all():
        raise ValueError('Expected a nonempty, finite 1-D array')
    ordered=np.sort(values)
    return (np.searchsorted(ordered,values,side='left')+
            np.searchsorted(ordered,values,side='right'))/(2.*len(values))


def joint_scores(temperature,rise,temperature_weight=.5):
    if not 0<temperature_weight<1:
        raise ValueError('Use a temperature weight strictly between 0 and 1')
    if len(temperature)!=len(rise):
        raise ValueError('Array lengths differ')
    pt=midpoint_percentiles(temperature)
    pd=midpoint_percentiles(rise)
    scores=100/(temperature_weight/pt+(1-temperature_weight)/pd)
    scores=np.where(np.asarray(rise)>0,scores,0.)
    return pt,pd,scores


def deviation_weights(temperature,rise,strength=.5):
    if not np.isfinite(strength) or strength<0:
        raise ValueError('Deviation strength must be finite and nonnegative')
    arrays=[np.asarray(temperature,dtype='f8'),np.asarray(rise,dtype='f8')]
    if arrays[0].shape!=arrays[1].shape:
        raise ValueError('Array shapes differ')
    statistics={};zs=[]
    for name,values in zip(('temperature','rise'),arrays):
        if values.ndim!=1 or not len(values) or not np.isfinite(values).all():
            raise ValueError('Expected nonempty finite 1-D arrays')
        mean=float(values.mean())
        sd=0. if np.all(values==values[0]) else float(values.std(ddof=0))
        z=(values-mean)/sd if sd>0 else np.zeros_like(values)
        statistics[name]={'mean_K':mean,'population_sd_K':sd}
        zs.append(z)
    boost=1+strength*np.sqrt(np.maximum(zs[0],0)*np.maximum(zs[1],0))
    return statistics,zs[0],zs[1],boost


def population_scores(temperature, rise, temperature_weight=.5, deviation_strength=.5):
    pt,pd,base=joint_scores(temperature,rise,temperature_weight)
    statistics,zt,zd,boost=deviation_weights(temperature,rise,deviation_strength)
    return {'temperature_percentile':pt*100,'rise_percentile':pd*100,
            'temperature_z':zt,'rise_z':zd,'base_joint_score':base,
            'deviation_weight':boost,'joint_score':base*boost,
            'population_statistics':statistics}
