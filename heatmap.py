"""All valid ROK pixel footprints: temporal change and current B07 BT."""
import base64
import io
import numpy as np
from scoring import population_scores
from render_lock import serialized_render

BOUNDS=(124.3,132.2,32.8,38.9)
RISE_LIMIT_K=5.0
TEMPERATURE_LIMITS_K=(220.,330.)


def mercator_lat(lat):
    return np.log(np.tan(np.pi/4+np.deg2rad(lat)/2))*180/np.pi


def heatmap_data(previous,current,geography):
    # Compare_frames validates the pair before this function is called.
    valid=geography.mask & np.isfinite(previous.bt) & np.isfinite(current.bt)
    corners=geography.pixel_corners(valid)
    if not valid.any() or not np.isfinite(corners).all():
        raise ValueError('No valid heatmap pixels, or invalid footprint navigation')
    rise=current.bt[valid].astype('f8')-previous.bt[valid].astype('f8')
    temperature=current.bt[valid].astype('f8')
    return corners,rise,temperature


@serialized_render
def build_heatmap(previous,current,geography,scoring=None,score_scale_max=500,pixel_fields=None):
    """Two transparent geographic overlays plus exact display statistics."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.collections import PolyCollection
    from matplotlib.colors import Normalize
    corners,rise,temperature=heatmap_data(previous,current,geography)
    config=scoring or {}
    if not np.isfinite(score_scale_max) or score_scale_max<=0:
        raise ValueError('Score scale maximum must be finite and positive')
    scores=population_scores(temperature,rise,config.get('temperature_weight',.5),config.get('deviation_strength',.5))['joint_score']
    extra=[]
    if pixel_fields is not None:
        valid=geography.mask & np.isfinite(previous.bt) & np.isfinite(current.bt)
        rows,cols=np.nonzero(valid)
        lookup={(p['native_row_1based'],p['native_column_1based']):p for p in pixel_fields}
        def field(name):
            return np.array([lookup[(int(r+current.first_line),int(c+1))].get(name) for r,c in zip(rows,cols)],dtype=float)
        scores=field('joint_score')
        extra=[('local',field('local_excess_K'),'YlOrRd',(0.,20.)),
               ('past',field('past_excess_K'),'YlOrRd',(0.,20.))]
    projected=corners.copy();projected[:,:,1]=mercator_lat(projected[:,:,1])
    west,east,south,north=BOUNDS
    bottom,top=mercator_lat(np.array([south,north]))
    layers={}
    for name,values,cmap,limits in (
        ('score',scores,'YlOrRd',(0.,score_scale_max)),
        ('rise',rise,'RdBu_r',(-RISE_LIMIT_K,RISE_LIMIT_K)),
        ('temperature',temperature,'inferno',TEMPERATURE_LIMITS_K),*extra):
        fig=plt.figure(figsize=(8,8),dpi=160)
        try:
            ax=fig.add_axes([0,0,1,1]);ax.set_axis_off()
            ax.set(xlim=(west,east),ylim=(bottom,top))
            ax.add_collection(PolyCollection(projected,array=values,cmap=cmap,
                norm=Normalize(*limits),edgecolors='none',antialiased=False))
            stream=io.BytesIO();fig.savefig(stream,format='png',transparent=True,dpi=160)
            layers[name]={'image':'data:image/png;base64,'+base64.b64encode(stream.getvalue()).decode('ascii'),
                'min':float(np.nanmin(values)) if np.isfinite(values).any() else None,'max':float(np.nanmax(values)) if np.isfinite(values).any() else None,
                'unit':'score' if name=='score' else 'K',
                'scale_min':limits[0],'scale_max':limits[1],
                'min_K':float(np.nanmin(values)) if name!='score' and np.isfinite(values).any() else None,'max_K':float(np.nanmax(values)) if name!='score' and np.isfinite(values).any() else None,
                'scale_min_K':limits[0] if name!='score' else None,'scale_max_K':limits[1] if name!='score' else None,
                'below_scale_pixels':int((values<limits[0]).sum()),
                'above_scale_pixels':int((values>limits[1]).sum())}
        finally: plt.close(fig)
    return {'valid_pixels':len(rise),'rising_pixels':int((rise>0).sum()),
        'falling_pixels':int((rise<0).sum()),'unchanged_pixels':int((rise==0).sum()),
        'projection':'Web Mercator (longitude / mercator latitude in degrees)',
        'bounds':[west,float(-top),east-west,float(top-bottom)],
        'coverage':'valid paired pixels whose centers lie in the configured ROK land mask',
        'resampling':'native pixel corner polygons; no interpolation or smoothing',
        **layers}


@serialized_render
def render_heatmap_png(result,features):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize
    from matplotlib.cm import ScalarMappable
    heat=result['heatmap']
    west,east,south,north=BOUNDS
    bottom,top=mercator_lat(np.array([south,north]))
    fig,axes=plt.subplots(1,3,figsize=(18,8),layout='constrained')
    try:
        for ax,name,cmap,label in zip(axes,('score','rise','temperature'),('YlOrRd','RdBu_r','inferno'),
                ('Final score','Change from previous observation (K)','Current brightness temperature (K)')):
            layer=heat[name]
            blob=base64.b64decode(layer['image'].split(',',1)[1])
            image=plt.imread(io.BytesIO(blob),format='png')
            ax.set_facecolor('#e5edf2')
            ax.imshow(image,extent=(west,east,bottom,top),origin='upper',interpolation='nearest',zorder=2)
            for f in features:
                g=f['geometry'];polys=g['coordinates'] if g['type']=='MultiPolygon' else [g['coordinates']]
                for polygon in polys:
                    ring=np.asarray(polygon[0])
                    ax.plot(ring[:,0],mercator_lat(ring[:,1]),color='#596c7c',lw=.35,zorder=3)
            ax.set(xlim=(west,east),ylim=(bottom,top),xlabel='Longitude (E)')
            ticks=np.arange(33,39);ax.set_yticks(mercator_lat(ticks),labels=[str(v) for v in ticks])
            ax.set_ylabel('Latitude (N)');ax.grid(alpha=.15);ax.set_title(label,fontsize=11)
            fig.colorbar(ScalarMappable(norm=Normalize(layer['scale_min'],layer['scale_max']),cmap=cmap),
                ax=ax,shrink=.75,extend='max' if name=='score' else 'both',label=layer['unit'])
        prev=result['previous_nominal_kst'];now=result['current_nominal_kst']
        quality='DIAGNOSTIC ONLY: UNLOADING FLAG | ' if result.get('quality_assessment',{}).get('diagnostic_only') else ''
        fig.suptitle(quality+f"B07 | All {heat['valid_pixels']:,} valid ROK land pixels\n"
                     f"{prev[:19]} -> {now[:19]} KST",fontsize=14)
        fig.supxlabel(f"JMA / NOAA | Natural Earth / geoBoundaries | No smoothing | Not a fire probability\n"
            f"Metric: {result.get('scoring',{}).get('metric','legacy')}; "
            "colors saturate outside labeled scale; missing/outside-land pixels are transparent",fontsize=8)
        stream=io.BytesIO();fig.savefig(stream,format='png',dpi=160)
        return stream.getvalue()
    finally: plt.close(fig)
